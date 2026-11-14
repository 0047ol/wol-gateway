using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Pipes;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WolPC {
    // User-session UI. No configuration, password or TLS credentials are read here.
    public static class TrayConnection {
        public static Dictionary<string,object> Read(byte command, string token) {
            return ReadFrom(Maintenance.PipeName,command,token);
        }
        public static Dictionary<string,object> ReadFrom(string pipeName,byte command, string token) {
            using(var pipe=new NamedPipeClientStream(".",pipeName,PipeDirection.InOut,PipeOptions.Asynchronous,TokenImpersonationLevel.Impersonation)) {
                pipe.Connect(1500); pipe.WriteByte(command);
                if(command==2) {
                    if(!Regex.IsMatch(token ?? "","^[0-9a-f]{64}$")) throw new ArgumentException("Invalid binding snapshot");
                    byte[] bytes=Encoding.ASCII.GetBytes(token); pipe.Write(bytes,0,bytes.Length);
                }
                byte[] buffer=new byte[4096]; int count=0;
                var deadline=DateTime.UtcNow.AddMilliseconds(2500);
                do {
                    int remaining=(int)(deadline-DateTime.UtcNow).TotalMilliseconds;
                    if(remaining<=0 || count==buffer.Length) throw new IOException("Service response timeout or overflow");
                    var pending=pipe.ReadAsync(buffer,count,buffer.Length-count);
                    if(!pending.Wait(remaining) || pending.Result<=0) throw new IOException("Incomplete service response");
                    count+=pending.Result;
                } while(buffer[count-1]!=10);
                return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(buffer,0,count));
            }
        }
    }
    public sealed class TrayState {
        public bool First=true, CanUnbind, CanUpdate;
        public string Binding="", Version="", Update="", Unlink="", Token="";
        public readonly List<string> Notices=new List<string>();
        public bool Prompt;
        public void Apply(Dictionary<string,object> state) {
            Notices.Clear(); Prompt=false;
            string binding=Convert.ToString(state["binding"]), unlink=Convert.ToString(state["unlink"]);
            string update=Convert.ToString(state["update"]), version=Convert.ToString(state["version"]);
            bool reachable=Convert.ToBoolean(state["reachable"]);
            Token=Convert.ToString(state["binding_token"]);
            bool busy=update=="checking" || update=="downloading" || update=="installing";
            CanUnbind=reachable && binding=="bound" && Regex.IsMatch(Token,"^[0-9a-f]{64}$") && unlink!="pending" && !busy;
            CanUpdate=!busy && unlink!="pending";
            if(unlink!=Unlink) {
                if(unlink=="success") { Notices.Add("WOL关机服务已解绑；设备记录和配置已保留，网关在线后恢复探测。"); Binding="unbound"; First=false; }
                if(unlink=="conflict") Notices.Add("关联状态已变化，请重新确认后解除关联。");
                if(unlink=="failed") Notices.Add("解除关联结果未确认，请等待网络恢复后查看状态；不会自动重试。");
            }
            Unlink=unlink;
            if(First && binding=="unbound") { Prompt=true; First=false; Binding="unbound"; }
            else if(reachable && (binding=="bound" || binding=="unbound")) {
                if(Binding!="" && Binding!=binding) Notices.Add(binding=="bound"?"已关联WOL关机服务，可以通过网页控制关机。":"WOL关机服务已解绑；如需使用，请在网页重新关联。");
                First=false;
            }
            if(reachable && (binding=="bound" || binding=="unbound")) Binding=binding;
            if(Version!="" && Version!=version) Notices.Add("已自动更新到 "+version+"，原有配置已保留。");
            Version=version;
            if(Update!="" && update!=Update) {
                if(update=="current") Notices.Add("当前已是最新版本 "+version);
                if(update=="downloading") Notices.Add("发现新版本，正在下载并自动安装。");
                if(update=="failed") Notices.Add("更新检查或安装失败，后台会自动重试，也可通过更新菜单重试。");
            }
            Update=update;
        }
    }
    public sealed class Desktop : ApplicationContext {
        readonly NotifyIcon icon=new NotifyIcon();
        readonly ContextMenuStrip menu=new ContextMenuStrip();
        readonly ToolStripMenuItem status=new ToolStripMenuItem("正在读取服务状态…");
        readonly ToolStripMenuItem startup=new ToolStripMenuItem("自启动"), update=new ToolStripMenuItem("更新");
        readonly ToolStripMenuItem unbind=new ToolStripMenuItem("解除关联"), uninstall=new ToolStripMenuItem("卸载"), exit=new ToolStripMenuItem("退出");
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
        readonly Control dispatch=new Control();
        readonly TrayState state=new TrayState();
        bool busy, hadService; volatile bool stopped; int missing;
        static bool restart;
        readonly EventWaitHandle close;
        public static bool Confirm(string message) { return MessageBox.Show(message,"WOL关机服务",MessageBoxButtons.YesNo,MessageBoxIcon.Question,MessageBoxDefaultButton.Button2)==DialogResult.Yes; }
        public static void Run() {
            bool created;
            using(var mutex=new Mutex(true,@"Local\WolPCService.Tray",out created)) {
                if(!created) return;
                try { Application.EnableVisualStyles(); Application.Run(new Desktop()); }
                finally { mutex.ReleaseMutex(); }
            }
            if(restart) Platform.StartTray();
        }
        public static void Launch() {
            if(!File.Exists(Platform.InstalledExe)) throw new IOException(Installer.RepairRequired);
            // A user-session copy keeps the installed service EXE replaceable.
            // It carries no credentials and never runs with service privileges.
            string folder=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),@"WolPCService\tray",Maintenance.Version);
            Platform.AssertTree(folder); Directory.CreateDirectory(folder);
            string copy=Path.Combine(folder,StorageSettings.ExecutableFileName);
            if(!File.Exists(copy) || !Maintenance.HashMatches(copy,FileHash(Platform.InstalledExe))) {
                try { File.Copy(Platform.InstalledExe,copy,true); } catch(IOException) { if(!Maintenance.HashMatches(copy,FileHash(Platform.InstalledExe))) throw; }
            }
            using(var process=Platform.Launch(copy,new[]{"--tray-host"},false,false)) {}
            foreach(string prior in Directory.GetDirectories(Path.GetDirectoryName(folder))) {
                if(prior==folder || !Regex.IsMatch(Path.GetFileName(prior),@"^\d+\.\d+\.\d+$")) continue;
                try {
                    Platform.AssertTree(prior);
                    string old=Path.Combine(prior,StorageSettings.ExecutableFileName);
                    if(File.Exists(old)) File.Delete(old);
                    if(Directory.GetFileSystemEntries(prior).Length==0) Directory.Delete(prior);
                } catch(IOException) {} catch(UnauthorizedAccessException) {}
            }
        }
        static string FileHash(string path) { using(var sha=System.Security.Cryptography.SHA256.Create()) using(var file=File.OpenRead(path)) return BitConverter.ToString(sha.ComputeHash(file)).Replace("-","").ToLowerInvariant(); }
        public Desktop() {
            dispatch.CreateControl();
            close=new EventWaitHandle(false,EventResetMode.ManualReset,@"Local\WolPCService.Tray.Exit"); close.Reset();
            status.Enabled=false; unbind.Enabled=false;
            menu.Items.AddRange(new ToolStripItem[]{status,new ToolStripSeparator(),startup,update,unbind,uninstall,exit});
            icon.Icon=SystemIcons.Application; icon.Text="WOL关机服务 · 正在读取状态"; icon.ContextMenuStrip=menu; icon.Visible=true;
            startup.Click+=delegate { Admin(new string[]{"--startup",startup.Checked?"off":"on"}); };
            update.Click+=delegate { Poll(1); };
            unbind.Click+=delegate {
                string token=state.Token;
                if(state.CanUnbind && Confirm("确定解除当前电脑的所有WOL关机服务关联？设备记录和本机配置会保留，后续上报不会自动重新关联。")) Admin(new string[]{"--unbind",token});
            };
            uninstall.Click+=delegate { if(Confirm("确定卸载WOL关机服务？将停止服务并移除托盘自启动，现有配置会保留。")) Admin(new string[]{"--uninstall","--confirmed"}); };
            exit.Click+=delegate { if(Confirm("确定退出托盘？后台WOL关机服务仍会继续运行，网页关机功能不受影响。")) ExitThread(); };
            menu.Opening+=delegate { Poll(0); };
            timer.Interval=3000; timer.Tick+=delegate { if(close.WaitOne(0)) ExitThread(); else Poll(0); };
            timer.Start(); Poll(0);
        }
        void Notice(string message) { icon.ShowBalloonTip(8000,"WOL关机服务",message,ToolTipIcon.Info); }
        void Admin(string[] args) {
            try { Platform.Launch(Platform.InstalledExe,args,true,false); }
            catch(Exception) { Notice("操作未执行，请允许管理员授权后重试。"); }
        }
        void Poll(byte command) {
            if(busy || stopped) return; busy=true;
            // SCM/pipe I/O runs outside the UI thread, including when opening menus.
            Task.Factory.StartNew(delegate {
                Dictionary<string,object> result=null; bool exists=false, auto=false;
                try {
                    using(var key=Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\WolPCService")) { exists=key!=null; if(exists) auto=Convert.ToInt32(key.GetValue("Start",3))==2; }
                    if(exists) result=TrayConnection.Read(command,null);
                } catch {}
                if(stopped) return;
                try { dispatch.BeginInvoke((Action)delegate {
                    if(stopped) return; busy=false; startup.Checked=auto;
                    if(!exists) { unbind.Enabled=false; status.Text="服务未安装"; if(hadService && ++missing>=2) ExitThread(); return; }
                    hadService=true; missing=0;
                    if(result==null) { unbind.Enabled=false; status.Text="服务连接中…"; return; }
                    if(Convert.ToString(result["version"])!=Maintenance.Version) { restart=true; ExitThread(); return; }
                    state.Apply(result); unbind.Enabled=state.CanUnbind; update.Enabled=state.CanUpdate;
                    unbind.Text=state.Unlink=="pending"?"正在解除关联…":"解除关联";
                    string binding=Convert.ToString(result["binding"]);
                    string label=binding=="bound"?"已关联":binding=="unbound"?"未关联":"正在确认关联状态";
                    status.Text=label+(Convert.ToBoolean(result["reachable"])?" · 服务运行中":" · 等待网络");
                    icon.Text="WOL关机服务 "+state.Version+" · "+label;
                    icon.Icon=Convert.ToBoolean(result["reachable"]) && binding=="bound"?SystemIcons.Application:SystemIcons.Information;
                    update.Text=state.Update=="checking"?"正在检查更新…":state.Update=="downloading"?"正在下载更新…":state.Update=="installing"?"正在安装更新…":state.Update=="failed"?"更新（上次失败，点击重试）":"更新";
                    foreach(string text in state.Notices) Notice(text);
                    if(state.Prompt) MessageBox.Show("当前电脑尚未关联WOL关机服务。请在网页添加本机局域网设备，然后通过电源按钮确认关联。已解绑的设备需要在网页重新确认，服务不会自动绑定。","WOL关机服务",MessageBoxButtons.OK,MessageBoxIcon.Information);
                }); } catch(InvalidOperationException) {}
            });
        }
        protected override void ExitThreadCore() {
            stopped=true; timer.Stop(); timer.Dispose(); icon.Visible=false; icon.Dispose(); menu.Dispose(); dispatch.Dispose(); close.Dispose(); base.ExitThreadCore();
        }
    }
}
