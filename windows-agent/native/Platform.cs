using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Management;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Threading;
using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace WolPC {
    public sealed class ServiceState {
        public bool Exists, Running;
        public int Startup=2;
        public string Image="", Tray="";
        public bool? RecoveryEnabled;
    }
    public sealed class StartupPlan {
        public int Mode;
        public bool Run, Tray;
        public static StartupPlan For(ServiceState state) {
            return new StartupPlan {Mode=state.Exists?state.Startup:2,Run=!state.Exists || state.Running,Tray=!state.Exists || state.Startup==2 && !String.IsNullOrEmpty(state.Tray)};
        }
    }
    public interface IInstallHost {
        ServiceState Capture();
        void Prepare();
        void Stop(ServiceState original);
        void Configure(ServiceState original);
        void Restore(ServiceState original);
        void Remove(ServiceState original);
        void Enroll(string executable);
        void Commit();
    }
    public static class Platform {
        public const string ServiceName="WolPCService";
        public const string RunKey=@"SOFTWARE\Microsoft\Windows\CurrentVersion\Run";
        public static string TrayQuery { get { return "SELECT ProcessId,ExecutablePath,CommandLine FROM Win32_Process WHERE Name=\""+StorageSettings.ExecutableFileName+"\""; } }
        public static string InstallDirectory { get { return Path.GetFullPath(Environment.ExpandEnvironmentVariables(StorageSettings.InstallDirectory)); } }
        public static string InstalledExe { get { return Path.Combine(InstallDirectory,StorageSettings.ExecutableFileName); } }
        public static bool Administrator { get { using(var identity=WindowsIdentity.GetCurrent()) return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator); } }
        public static bool Same(string a,string b) { return String.Equals(Path.GetFullPath(a),Path.GetFullPath(b),StringComparison.OrdinalIgnoreCase); }
        // Windows CreateProcess quoting, including trailing backslashes and quotes.
        public static string Quote(string value) {
            var text=new System.Text.StringBuilder("\""); int slashes=0;
            foreach(char ch in value) {
                if(ch=='\\') { slashes++; continue; }
                if(ch=='"') text.Append('\\',slashes*2+1); else text.Append('\\',slashes);
                slashes=0; text.Append(ch);
            }
            return text.Append('\\',slashes*2).Append('"').ToString();
        }
        public static Process Launch(string executable,string[] args,bool elevate,bool wait) {
            var info=new ProcessStartInfo(executable,String.Join(" ",args.Select(Quote))) {
                UseShellExecute=elevate, CreateNoWindow=!elevate, WindowStyle=ProcessWindowStyle.Hidden
            };
            if(elevate) info.Verb="runas";
            var process=Process.Start(info); if(process==null) throw new IOException("Process did not start");
            if(wait && !process.WaitForExit(90000)) { process.Dispose(); throw new IOException("Operation timed out; check service status before retrying"); }
            return process;
        }
        public static int Elevated(string[] args) {
            using(var process=Launch(System.Windows.Forms.Application.ExecutablePath,args,true,true)) return process.ExitCode;
        }
        public static int Command(string name,params string[] args) {
            using(var process=Launch(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),name),args,false,true)) return process.ExitCode;
        }
        public static void Sc(params string[] args) { int code=Command("sc.exe",args); if(code!=0) throw new Win32Exception(code); }
        public static void AssertTree(string path) {
            string current=Path.GetFullPath(path);
            while(!String.IsNullOrEmpty(current)) {
                if((Directory.Exists(current) || File.Exists(current)) && (File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0) throw new IOException("Links and junctions are not allowed");
                current=Path.GetDirectoryName(current);
            }
            if(!Directory.Exists(path)) return;
            foreach(string child in Directory.GetFileSystemEntries(path)) {
                if((File.GetAttributes(child)&FileAttributes.ReparsePoint)!=0) throw new IOException("Links and junctions are not allowed");
                if(Directory.Exists(child)) AssertTree(child);
            }
        }
        public static void SecureDirectory(string path,bool read) {
            AssertTree(path); Directory.CreateDirectory(path);
            var acl=new DirectorySecurity(); acl.SetAccessRuleProtection(true,false);
            var admin=new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null); acl.SetOwner(admin);
            foreach(var sid in new[]{new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),admin})
                acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            if(read) acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid,null),FileSystemRights.ReadAndExecute,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            new DirectoryInfo(path).SetAccessControl(acl);
            foreach(string child in Directory.GetFileSystemEntries(path)) {
                if(Directory.Exists(child)) SecureDirectory(child,read);
                else {
                    var fileAcl=new FileSecurity(); fileAcl.SetAccessRuleProtection(true,false); fileAcl.SetOwner(admin);
                    foreach(var sid in new[]{new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),admin}) fileAcl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,AccessControlType.Allow));
                    if(read) fileAcl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinUsersSid,null),FileSystemRights.ReadAndExecute,AccessControlType.Allow));
                    new FileInfo(child).SetAccessControl(fileAcl);
                }
            }
        }
        public static void ValidateLocation(string target,ServiceState original) {
            if(!Path.IsPathRooted(target) || target.StartsWith(@"\\") || Path.GetFullPath(target).TrimEnd('\\')==Path.GetPathRoot(target).TrimEnd('\\')) throw new IOException("Invalid installation location");
            string windows=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            if(Path.GetFullPath(target).StartsWith(windows.TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)) throw new IOException("Installation in the Windows directory is not allowed");
            if(original.Exists) {
                string image=ImageFile(original.Image);
                bool legacy=Same(image,Path.Combine(windows,@"WolPCService\WolPCService.exe")) && Same(target,Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"WolPCService"));
                if(!Same(image,Path.Combine(target,StorageSettings.ExecutableFileName)) && !legacy) throw new IOException("The existing service uses a different path; configuration has been kept");
                AssertTree(Path.GetDirectoryName(image));
            }
        }
        public static string ImageFile(string image) {
            image=Environment.ExpandEnvironmentVariables(image.Trim());
            if(image.StartsWith("\"") && image.EndsWith("\"")) image=image.Substring(1,image.Length-2);
            if(image.Contains("\"") || !Path.IsPathRooted(image) || !image.EndsWith(".exe",StringComparison.OrdinalIgnoreCase)) throw new IOException("Unexpected service executable arguments");
            return Path.GetFullPath(image);
        }
        public static void StartTray() { using(var p=Launch(InstalledExe,new[]{"--tray"},false,false)) {} }
    }
    public sealed class WindowsHost : IInstallHost {
        static bool? RecoveryState() {
            object scheduler=null,folder=null,task=null;
            try {
                scheduler=Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service",true));
                scheduler.GetType().InvokeMember("Connect",BindingFlags.InvokeMethod,null,scheduler,new object[0]);
                folder=scheduler.GetType().InvokeMember("GetFolder",BindingFlags.InvokeMethod,null,scheduler,new object[]{@"\"});
                task=folder.GetType().InvokeMember("GetTask",BindingFlags.InvokeMethod,null,folder,new object[]{"WolPCService-Recovery"});
                return Convert.ToBoolean(task.GetType().InvokeMember("Enabled",BindingFlags.GetProperty,null,task,null));
            } catch(Exception error) {
                var failure=error is TargetInvocationException?error.InnerException:error;
                if(failure.HResult==unchecked((int)0x80070002)) return null;
                throw;
            } finally { foreach(object item in new[]{task,folder,scheduler}) if(item!=null && Marshal.IsComObject(item)) Marshal.FinalReleaseComObject(item); }
        }
        static void Recovery(bool enabled) {
            int code=Platform.Command("schtasks.exe","/Change","/TN","WolPCService-Recovery",enabled?"/Enable":"/Disable");
            if(code!=0) throw new Win32Exception(code);
        }
        public ServiceState Capture() {
            var state=new ServiceState();
            using(var key=Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\WolPCService")) {
                state.Exists=key!=null;
                if(key!=null) { state.Image=Convert.ToString(key.GetValue("ImagePath")); state.Startup=Convert.ToInt32(key.GetValue("Start",3)); }
            }
            using(var key=Registry.LocalMachine.OpenSubKey(Platform.RunKey)) state.Tray=key==null?"":Convert.ToString(key.GetValue(Platform.ServiceName,""));
            if(state.Exists) using(var service=new ServiceController(Platform.ServiceName)) state.Running=service.Status!=ServiceControllerStatus.Stopped;
            state.RecoveryEnabled=RecoveryState();
            return state;
        }
        public void Prepare() {
            Platform.SecureDirectory(Platform.InstallDirectory,true); Platform.SecureDirectory(Config.Folder,false);
        }
        public void Stop(ServiceState original) {
            if(original.RecoveryEnabled==true) Recovery(false);
            if(original.Exists) using(var service=new ServiceController(Platform.ServiceName)) {
                service.Refresh();
                if(service.Status!=ServiceControllerStatus.Stopped) {
                    try { if(service.Status!=ServiceControllerStatus.StopPending) service.Stop(); service.WaitForStatus(ServiceControllerStatus.Stopped,TimeSpan.FromSeconds(30)); }
                    catch(System.ServiceProcess.TimeoutException) { StopVerified(original); service.WaitForStatus(ServiceControllerStatus.Stopped,TimeSpan.FromSeconds(30)); }
                }
            }
            // Stop only our native tray; never kill by image name. Legacy PowerShell
            // trays don't lock the EXE and can retire after detecting version change.
            using(var search=new ManagementObjectSearcher(Platform.TrayQuery))
            using(var processes=search.Get()) foreach(ManagementObject record in processes) using(record) {
                string path=Convert.ToString(record["ExecutablePath"]), command=Convert.ToString(record["CommandLine"]);
                int pid=Convert.ToInt32(record["ProcessId"]);
                if(pid==Process.GetCurrentProcess().Id || String.IsNullOrEmpty(path) || !Platform.Same(path,Platform.InstalledExe) || !command.EndsWith("\"--tray\"",StringComparison.Ordinal) && !command.EndsWith(" --tray",StringComparison.Ordinal)) continue;
                using(var process=Process.GetProcessById(pid)) { if(Platform.Same(process.MainModule.FileName,Platform.InstalledExe)) { process.Kill(); process.WaitForExit(5000); } }
            }
        }
        static void StopVerified(ServiceState original) {
            using(var service=new ManagementObject("Win32_Service.Name='WolPCService'")) {
                service.Get(); int pid=Convert.ToInt32(service["ProcessId"]);
                if(pid<=0 || pid==Process.GetCurrentProcess().Id || !Platform.Same(Platform.ImageFile(Convert.ToString(service["PathName"])),Platform.ImageFile(original.Image))) throw new IOException("Cannot verify the unresponsive service process");
                using(var process=Process.GetProcessById(pid)) {
                    if(!Platform.Same(process.MainModule.FileName,Platform.ImageFile(original.Image))) throw new IOException("Service process image changed");
                    Platform.Sc("config",Platform.ServiceName,"start=","disabled"); process.Kill();
                    if(!process.WaitForExit(30000)) throw new IOException("The verified service did not stop");
                }
            }
        }
        static void SetTray(string value) {
            using(var key=Registry.LocalMachine.CreateSubKey(Platform.RunKey)) {
                if(String.IsNullOrEmpty(value)) key.DeleteValue(Platform.ServiceName,false); else key.SetValue(Platform.ServiceName,value,RegistryValueKind.String);
            }
        }
        static string StartMode(int mode) { return mode==2?"auto":mode==4?"disabled":"demand"; }
        static void Start(int startup,bool run) {
            Platform.Sc("config",Platform.ServiceName,"start=",run && startup==4?"demand":StartMode(startup));
            if(run) using(var service=new ServiceController(Platform.ServiceName)) {
                service.Refresh(); if(service.Status!=ServiceControllerStatus.Running) service.Start();
                service.WaitForStatus(ServiceControllerStatus.Running,TimeSpan.FromSeconds(30));
                // Running must remain stable; an immediate crash isn't an install success.
                Thread.Sleep(1000); service.Refresh(); if(service.Status!=ServiceControllerStatus.Running) throw new IOException("Service exited after startup");
            }
            if(startup==4 && run) Platform.Sc("config",Platform.ServiceName,"start=","disabled");
        }
        public void Configure(ServiceState original) {
            if(!original.Exists) Platform.Sc("create",Platform.ServiceName,"binPath=",Platform.Quote(Platform.InstalledExe),"start=","demand","DisplayName=","WOL关机服务");
            else Platform.Sc("config",Platform.ServiceName,"binPath=",Platform.Quote(Platform.InstalledExe),"DisplayName=","WOL关机服务");
            Platform.Sc("description",Platform.ServiceName,"WOL关机服务");
            Platform.Sc("sdset",Platform.ServiceName,"D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;CCLCSWLOCRRC;;;AU)");
            Platform.Sc("failure",Platform.ServiceName,"reset=","86400","actions=","restart/5000/restart/10000/restart/30000");
            Platform.Sc("failureflag",Platform.ServiceName,"1");
            var plan=StartupPlan.For(original);
            Start(plan.Mode,plan.Run);
            SetTray(plan.Tray?Platform.Quote(Platform.InstalledExe)+" --tray":"");
        }
        public void Restore(ServiceState original) {
            if(!original.Exists) { var current=Capture(); if(current.Exists) RemoveService(current); SetTray(original.Tray); if(original.RecoveryEnabled.HasValue) Recovery(original.RecoveryEnabled.Value); return; }
            Platform.Sc("config",Platform.ServiceName,"binPath=",original.Image);
            Start(original.Startup,original.Running); SetTray(original.Tray);
            if(original.RecoveryEnabled.HasValue) Recovery(original.RecoveryEnabled.Value);
        }
        public void Remove(ServiceState original) {
            RemoveService(original); Commit();
        }
        void RemoveService(ServiceState original) {
            if(original.Exists) { Stop(original); Platform.Sc("delete",Platform.ServiceName); }
            SetTray("");
        }
        public void Commit() {
            if(RecoveryState().HasValue) {
                int code=Platform.Command("schtasks.exe","/Delete","/TN","WolPCService-Recovery","/F"); if(code!=0) throw new Win32Exception(code);
            }
        }
        public void Enroll(string executable) {
            using(var process=Platform.Launch(executable,new[]{"--configure"},false,true)) {
                if(process.ExitCode!=0) throw new IOException("设备注册未完成，配置已保留。请确认网页已添加本机设备，再重新安装。");
            }
        }
        public static void Startup(bool enabled) {
            Platform.Sc("config",Platform.ServiceName,"start=",enabled?"auto":"demand");
            SetTray(enabled?Platform.Quote(Platform.InstalledExe)+" --tray":"");
            if(enabled) Start(2,true);
        }
    }
}
