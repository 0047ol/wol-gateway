using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WolPC {
    public sealed class InstallTransaction {
        public static readonly string[] Legacy={StorageSettings.ExecutableFileName+".config","enroll_ca.pem","Install.ps1","Uninstall.ps1","Tray.ps1","Control.ps1","Update.ps1","Install.cmd","Uninstall.cmd","Diagnose.cmd","Diagnose.ps1","EnableStartup.cmd","DisableStartup.cmd","Startup.ps1"};
        readonly string target,data,version;
        readonly IInstallHost host;
        public InstallTransaction(string target,string data,IInstallHost host,string version=null) { this.target=target; this.data=data; this.host=host; this.version=version ?? Maintenance.Version; }
        public void Install(string source,bool update) {
            var original=host.Capture();
            if(update && !original.Exists) throw new IOException("Service was uninstalled; update cancelled");
            host.Prepare();
            string marker=Path.Combine(data,"updates","last-result.txt");
            if(update && File.Exists(marker) && File.ReadAllText(marker).StartsWith("rollback_failed:")) throw new IOException("Manual repair required; recovery files retained");
            string recovery=Path.Combine(data,"recovery","install-"+Guid.NewGuid().ToString("N")); Directory.CreateDirectory(recovery);
            var names=new[]{StorageSettings.ExecutableFileName}.Concat(Legacy).ToArray();
            var saved=new List<string>(); bool changed=false;
            try {
                foreach(string name in names) {
                    string file=Path.Combine(target,name);
                    if(File.Exists(file)) { File.Copy(file,Path.Combine(recovery,name),false); saved.Add(name); }
                }
                // This manifest is private; it contains the service/startup snapshot,
                // never service credentials. Keep it with backups until commit.
                File.WriteAllText(Path.Combine(recovery,"state.json"),new JavaScriptSerializer().Serialize(original),Encoding.UTF8);
                changed=true; host.Stop(original);
                string destination=Path.Combine(target,StorageSettings.ExecutableFileName);
                string pending=destination+".new";
                File.Copy(source,pending,true);
                if(File.Exists(destination)) File.Replace(pending,destination,null); else File.Move(pending,destination);
                if(!update) host.Enroll(destination);
                host.Configure(original);
                Directory.CreateDirectory(Path.GetDirectoryName(marker));
                File.WriteAllText(marker,(update?"updated:":"manual_repair:")+version);
            } catch(Exception error) {
                bool restored=true;
                if(changed) try {
                    var current=host.Capture(); host.Stop(current);
                    foreach(string name in names) {
                        string file=Path.Combine(target,name);
                        if(saved.Contains(name)) File.Copy(Path.Combine(recovery,name),file,true);
                        else if(File.Exists(file)) File.Delete(file);
                    }
                    string pending=Path.Combine(target,StorageSettings.ExecutableFileName+".new"); if(File.Exists(pending)) File.Delete(pending);
                    host.Restore(original);
                } catch { restored=false; }
                Directory.CreateDirectory(Path.GetDirectoryName(marker));
                File.WriteAllText(marker,(restored?"failed:":"rollback_failed:")+error.GetType().Name);
                // A failed installation keeps its exact recovery set for repair.
                throw new IOException(restored?"安装失败，已恢复原服务。配置已保留。":"安装及恢复失败，请保留恢复文件并手动修复。",error);
            }
            // Commit is complete. Cleanup errors must never roll back a healthy
            // service after some recovery files have already been removed.
            try {
                host.Commit();
                foreach(string name in Legacy) { string file=Path.Combine(target,name); if(File.Exists(file)) File.Delete(file); }
                foreach(string file in Directory.GetFiles(recovery)) File.Delete(file);
                Directory.Delete(recovery);
            } catch(Exception error) { LocalLog.Write("install_cleanup_pending type="+error.GetType().Name); }
        }
        public void Uninstall() {
            var original=host.Capture(); host.Remove(original);
            foreach(string name in new[]{StorageSettings.ExecutableFileName}.Concat(Legacy)) {
                string file=Path.Combine(target,name); if(File.Exists(file)) File.Delete(file);
            }
            // All business/credential configuration is deliberately kept.
        }
    }
    public static class Installer {
        public static string Resource(string name) {
            using(var stream=typeof(Installer).Assembly.GetManifestResourceStream(name)) {
                if(stream==null) throw new IOException("Missing embedded resource");
                using(var reader=new StreamReader(stream,Encoding.UTF8)) return reader.ReadToEnd();
            }
        }
        public static bool RuntimeReady() {
            using(var key=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full")) return key!=null && Convert.ToInt32(key.GetValue("Release",0))>=528040;
        }
        public static void ValidateUpdate(string executable,string hash,string version,string current) {
            if(!Regex.IsMatch(hash ?? "","^[0-9a-f]{64}$") || !Maintenance.Newer(version,current) || !Maintenance.HashMatches(executable,hash)) throw new InvalidDataException("Update hash or version mismatch");
            if(new FileInfo(executable).Length>16*1024*1024 || FileVersionInfo.GetVersionInfo(executable).FileVersion!=version+".0") throw new InvalidDataException("Unexpected update executable");
        }
        public static void SelfTest(string executable) {
            using(var process=Platform.Launch(executable,new[]{"--self-test"},false,true)) if(process.ExitCode!=0) throw new IOException("Incoming executable self-test failed");
        }
        public const string RepairRequired="服务注册记录仍在，但安装文件已缺失。请从网站重新下载WolPCService.exe，在下载位置双击进行修复安装；现有配置和设备关联会保留。";
        public static void ValidateExistingVersion(ServiceState original,string source,bool update) {
            if(!original.Exists) return;
            Version current;
            try { current=new Version(FileVersionInfo.GetVersionInfo(Platform.ImageFile(original.Image)).FileVersion); }
            catch(FileNotFoundException) { if(update) throw new IOException(RepairRequired); return; }
            catch(DirectoryNotFoundException) { if(update) throw new IOException(RepairRequired); return; }
            var incoming=new Version(FileVersionInfo.GetVersionInfo(source).FileVersion);
            if(incoming<current || update && incoming==current) throw new IOException("The installed version changed; update cancelled");
        }
        public static string Helper() {
            Platform.SecureDirectory(Config.Folder,false);
            string directory=Path.Combine(Config.Folder,"workers"); Platform.SecureDirectory(directory,false);
            // Previous helpers may be locked until their process exits; skip them.
            foreach(string old in Directory.GetFiles(directory,"worker-*.exe")) {
                if(Regex.IsMatch(Path.GetFileName(old),"^worker-[0-9a-f]{32}\\.exe$")) try { File.Delete(old); } catch(IOException) {} catch(UnauthorizedAccessException) {}
            }
            string path=Path.Combine(directory,"worker-"+Guid.NewGuid().ToString("N")+".exe");
            File.Copy(Application.ExecutablePath,path,false); return path;
        }
        public static void StartUpdate(string package,string hash,string version) {
            ValidateUpdate(package,hash,version,Maintenance.Version);
            SelfTest(package);
            string helper=Helper();
            File.WriteAllText(Path.Combine(Config.Folder,@"updates\last-result.txt"),"pending:"+version);
            using(var process=Platform.Launch(helper,new[]{"--apply-update",package,hash,version},false,false)) {
                if(process.WaitForExit(3000) && process.ExitCode!=0) throw new IOException("Updater did not start");
            }
        }
        static int Locked(Action operation) {
            bool created;
            using(var mutex=new Mutex(false,@"Global\WolPCService.NativeMaintenance.v1",out created)) {
                bool owns=false;
                try {
                    try { owns=mutex.WaitOne(0); } catch(AbandonedMutexException) { owns=true; }
                    if(!owns) throw new IOException("另一个安装、更新或卸载操作正在进行，请稍后重试。");
                    operation(); return 0;
                } finally { if(owns) mutex.ReleaseMutex(); }
            }
        }
        public static int Install(bool update,string source) {
            if(!Platform.Administrator) throw new UnauthorizedAccessException();
            var host=new WindowsHost(); Platform.ValidateLocation(Platform.InstallDirectory,host.Capture());
            Platform.AssertTree(Platform.InstallDirectory); Platform.AssertTree(Config.Folder);
            SelfTest(source);
            return Locked(delegate {
                var original=host.Capture();
                Platform.ValidateLocation(Platform.InstallDirectory,original);
                ValidateExistingVersion(original,source,update);
                string incomingVersion=new Version(FileVersionInfo.GetVersionInfo(source).FileVersion).ToString(3);
                new InstallTransaction(Platform.InstallDirectory,Config.Folder,host,incomingVersion).Install(source,update);
            });
        }
        public static int Handle(string[] args) {
            string command=args.Length==0?"":args[0];
            if(command=="--apply-update") {
                if(!Platform.Administrator || args.Length!=4) return 2;
                string package=Path.Combine(Config.Folder,@"updates\WolPCService.exe");
                if(!Platform.Same(args[1],package)) throw new IOException("Unexpected update source");
                Platform.AssertTree(Config.Folder);
                ValidateUpdate(package,args[2],args[3],Maintenance.Version);
                int result=Install(true,package);
                try { File.Delete(package); } catch(IOException) {} catch(UnauthorizedAccessException) {}
                return result;
            }
            if(command=="--worker-uninstall") {
                if(!Platform.Administrator || args.Length!=2) return 2;
                int parent; if(!Int32.TryParse(args[1],out parent)) return 2;
                try { using(var process=Process.GetProcessById(parent)) if(!process.WaitForExit(15000)) throw new IOException("Uninstaller launcher did not exit"); } catch(ArgumentException) {}
                var host=new WindowsHost(); Platform.ValidateLocation(Platform.InstallDirectory,host.Capture()); Platform.AssertTree(Platform.InstallDirectory); Platform.AssertTree(Config.Folder);
                return Locked(delegate { new InstallTransaction(Platform.InstallDirectory,Config.Folder,host).Uninstall(); });
            }
            if(command=="--uninstall") {
                if(!args.Contains("--confirmed") && !Desktop.Confirm("确定卸载WOL关机服务？现有配置和设备关联记录会保留。")) return 0;
                if(!Platform.Administrator) return Platform.Elevated(new[]{"--uninstall","--confirmed"});
                string helper=Helper();
                using(var process=Platform.Launch(helper,new[]{"--worker-uninstall",Process.GetCurrentProcess().Id.ToString()},false,false)) {}
                return 0;
            }
            if(command=="--startup") {
                if(args.Length!=2 || args[1]!="on" && args[1]!="off") return 2;
                if(!Platform.Administrator) return Platform.Elevated(args);
                return Locked(delegate { WindowsHost.Startup(args[1]=="on"); });
            }
            if(command=="--unbind") {
                if(args.Length!=2 || !Regex.IsMatch(args[1],"^[0-9a-f]{64}$")) return 2;
                if(!Platform.Administrator) return Platform.Elevated(args);
                var result=TrayConnection.Read(2,args[1]); string state=Convert.ToString(result["unlink"]);
                if(state!="pending" && state!="success") throw new IOException("关联状态已变化或服务正忙，请在托盘重新确认。");
                return 0;
            }
            if(command=="--install") {
                if(!Platform.Administrator) return Platform.Elevated(args);
                if(Platform.Same(Application.ExecutablePath,Platform.InstalledExe)) throw new IOException("请下载新版EXE后从下载位置运行，以免正在运行的程序阻止替换。");
                return Install(args.Contains("--update"),Application.ExecutablePath);
            }
            return -1;
        }
    }
}
