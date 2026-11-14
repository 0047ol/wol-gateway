using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Net;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

namespace WolPC {
    // Session 0 owns credentials and installation. The user-session tray only
    // reads sanitized state. Unlink additionally requires an elevated local caller.
    public sealed class Maintenance {
        public const string Version = "1.3.2";
        const string Endpoint = "https://YOUR_SERVER_HOST:8443";
        public const string PipeName = "WolPCService.Tray.v1";
        readonly string pipeName=PipeName;
        readonly ManualResetEvent stop = new ManualResetEvent(false);
        readonly object gate = new object();
        Thread worker, pipeWorker;
        NamedPipeServerStream pipe;
        string binding = "checking", update = "idle";
        string bindingToken = "", unlink = "idle", unlinkToken;
        bool reachable;
        long checkedAt;
        bool requestCheck;
        DateTime lastManual = DateTime.MinValue;
        DateTime nextUpdate = DateTime.UtcNow.AddSeconds(45);

        public void Start() {
            // Persist the retry window across service restarts and update rollback.
            // A broken release must never put the computer in a restart loop.
            try {
                string directory=Path.Combine(Config.Folder,"updates");
                long ticks;
                if(Int64.TryParse(File.ReadAllText(Path.Combine(directory,"last-attempt.txt")),out ticks)) {
                    var retry=new DateTime(ticks,DateTimeKind.Utc).AddHours(6);
                    if(retry>nextUpdate && retry<DateTime.UtcNow.AddHours(7)) nextUpdate=retry;
                }
            } catch(IOException) {} catch(ArgumentException) {} catch(UnauthorizedAccessException) {}
            try {
                string result=File.ReadAllText(Path.Combine(Config.Folder,"updates","last-result.txt"));
                if(result.StartsWith("failed:") || result.StartsWith("rollback_failed:")) update="failed";
            } catch(IOException) {} catch(UnauthorizedAccessException) {}
            worker = new Thread(Run) {IsBackground=true}; worker.Start();
            pipeWorker = new Thread(Serve) {IsBackground=true}; pipeWorker.Start();
        }
        public void Stop() {
            stop.Set(); lock(gate) { if(pipe!=null) pipe.Dispose(); }
            if(pipeWorker!=null) pipeWorker.Join(3000);
            if(worker!=null) worker.Join(1000);
        }
        public static bool Newer(string available, string current) {
            System.Version a, c;
            return Regex.IsMatch(available ?? "", @"^\d+\.\d+\.\d+$") &&
                System.Version.TryParse(available,out a) && System.Version.TryParse(current,out c) && a>c;
        }
        public static void ValidateRelease(Dictionary<string,object> release) {
            string version=Convert.ToString(release["version"]), hash=Convert.ToString(release["sha256"]);
            long size=Convert.ToInt64(release["size"]);
            if(!Regex.IsMatch(version,@"^\d+\.\d+\.\d+$") || !Regex.IsMatch(hash,"^[0-9a-f]{64}$") || size<1 || size>16*1024*1024)
                throw new InvalidDataException("Invalid release metadata");
        }
        public static bool HashMatches(string path, string expected) {
            using(var sha=SHA256.Create()) using(var file=File.OpenRead(path))
                return BitConverter.ToString(sha.ComputeHash(file)).Replace("-","").ToLowerInvariant()==expected;
        }
        HttpWebRequest Request(Config config, string path) {
            ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
            var r=(HttpWebRequest)WebRequest.Create(Endpoint+path);
            r.Proxy=null; r.AllowAutoRedirect=false; r.Timeout=15000; r.ReadWriteTimeout=15000;
            r.ServerCertificateValidationCallback=Trust.Validate;
            r.Headers[HttpRequestHeader.Authorization]="Basic "+Convert.ToBase64String(Encoding.UTF8.GetBytes(config.Username+":"+config.Password));
            return r;
        }
        Dictionary<string,object> Fetch(Config config) {
            using(var response=Request(config,"/api/pc/runtime").GetResponse())
            using(var reader=new StreamReader(response.GetResponseStream())) {
                char[] buffer=new char[8193]; int count=reader.ReadBlock(buffer,0,buffer.Length);
                if(count>8192) throw new InvalidDataException("Oversize response");
                return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(new string(buffer,0,count));
            }
        }
        void Unbind(Config config, string token) {
            var request=Request(config,"/api/pc/unbind");
            request.Method="POST"; request.ContentType="application/json";
            byte[] body=Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(new {binding_token=token}));
            request.ContentLength=body.Length;
            using(var stream=request.GetRequestStream()) stream.Write(body,0,body.Length);
            using(var response=request.GetResponse())
            using(var reader=new StreamReader(response.GetResponseStream())) {
                char[] buffer=new char[1025]; int count=reader.ReadBlock(buffer,0,buffer.Length);
                if(count>1024) throw new InvalidDataException("Oversize response");
                var result=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(new string(buffer,0,count));
                if(!(bool)result["ok"] || (bool)result["bound"]) throw new InvalidDataException("Unconfirmed unlink");
            }
        }
        // Called only after named-pipe impersonation verifies the elevated caller.
        void QueueUnbind(string token, bool administrator) {
            lock(gate) {
                if(unlink=="pending") return;
                if(!administrator) return;
                if(!reachable || binding!="bound" || update=="checking" || update=="downloading" || update=="installing" || !Regex.IsMatch(token ?? "","^[0-9a-f]{64}$") || token!=bindingToken) {
                    unlink="conflict"; return;
                }
                unlinkToken=token; unlink="pending";
            }
        }
        void SetUpdate(string state) { lock(gate) update=state; }
        void Install(Config config, Dictionary<string,object> release) {
            ValidateRelease(release);
            string version=(string)release["version"], hash=(string)release["sha256"];
            long size=Convert.ToInt64(release["size"]);
            if(!Newer(version,Version)) { SetUpdate("current"); return; }
            SetUpdate("downloading");
            string directory=Path.Combine(Config.Folder,"updates"); Directory.CreateDirectory(directory);
            string resultFile=Path.Combine(directory,"last-result.txt");
            if(File.Exists(resultFile) && File.ReadAllText(resultFile).StartsWith("rollback_failed:"))
                throw new IOException("Manual installation repair required before another update");
            File.WriteAllText(Path.Combine(directory,"last-attempt.txt"),DateTime.UtcNow.Ticks.ToString());
            string package=Path.Combine(directory,"WolPCService.exe");
            using(var response=Request(config,"/api/pc/executable/"+version).GetResponse())
            using(var source=response.GetResponseStream())
            using(var target=new FileStream(package,FileMode.Create,FileAccess.Write,FileShare.None)) {
                byte[] buffer=new byte[16384]; int n; long total=0;
                while((n=source.Read(buffer,0,buffer.Length))>0) {
                    if(stop.WaitOne(0)) throw new OperationCanceledException();
                    total+=n; if(total>size) throw new InvalidDataException("Package size mismatch");
                    target.Write(buffer,0,n);
                }
                if(total!=size) throw new InvalidDataException("Truncated package");
            }
            if(!HashMatches(package,hash)) throw new InvalidDataException("Package hash mismatch");
            SetUpdate("installing");
            Installer.StartUpdate(package,hash,version);
        }
        void Run() {
            while(!stop.WaitOne(0)) {
                bool check; string token;
                lock(gate) {
                    token=unlinkToken; unlinkToken=null;
                    check=token==null && (requestCheck || DateTime.UtcNow>=nextUpdate);
                    requestCheck=false;
                    if(check) update="checking";
                }
                if(token!=null) {
                    try {
                        Unbind(Config.Load(),token);
                        lock(gate) { binding="unbound"; bindingToken=""; reachable=true; checkedAt=DateTime.UtcNow.Ticks; unlink="success"; }
                    } catch(WebException error) {
                        var response=error.Response as HttpWebResponse;
                        lock(gate) { unlink=response!=null && response.StatusCode==HttpStatusCode.Conflict?"conflict":"failed"; reachable=false; }
                        if(response!=null) response.Dispose();
                        LocalLog.Write("self_unbind_failed type="+error.GetType().Name);
                    } catch(Exception error) {
                        lock(gate) { unlink="failed"; reachable=false; }
                        LocalLog.Write("self_unbind_failed type="+error.GetType().Name);
                    }
                    // Never retry a mutation automatically after an uncertain result.
                    continue;
                }
                try {
                    var config=Config.Load();
                    lock(gate) {
                        if(update=="installing") {
                            string result=Path.Combine(Config.Folder,"updates","last-result.txt");
                            if(File.Exists(result) && (File.ReadAllText(result).StartsWith("failed:") || File.ReadAllText(result).StartsWith("rollback_failed:"))) update="failed";
                        }
                    }
                    if(check) SetUpdate("checking");
                    var status=Fetch(config);
                    bool bound=(bool)status["bound"];
                    object tokenValue;
                    string currentToken=status.TryGetValue("binding_token",out tokenValue)?Convert.ToString(tokenValue):"";
                    lock(gate) { binding=bound?"bound":"unbound"; bindingToken=currentToken; reachable=true; checkedAt=DateTime.UtcNow.Ticks; }
                    if(check) { nextUpdate=DateTime.UtcNow.AddHours(6); Install(config,(Dictionary<string,object>)status["executable_release"]); }
                } catch(Exception error) {
                    lock(gate) {
                        reachable=false;
                        if(!File.Exists(Config.FileName)) binding="unbound";
                        if(check) { update="failed"; nextUpdate=DateTime.UtcNow.AddHours(6); }
                    }
                    LocalLog.Write("maintenance_failed type="+error.GetType().Name);
                }
                // Manual checks wake within one second; binding is normally polled every 20s.
                for(int i=0;i<20 && !stop.WaitOne(1000);i++) { lock(gate) { if(requestCheck || unlinkToken!=null) break; } }
            }
        }
        string Snapshot() {
            lock(gate) return new JavaScriptSerializer().Serialize(new {version=Version,binding=binding,binding_token=bindingToken,unlink=unlink,reachable=reachable,checked_at=checkedAt,update=update});
        }
        void Serve() {
            while(!stop.WaitOne(0)) {
                try {
                    var security=new PipeSecurity();
                    security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.NetworkSid,null),PipeAccessRights.FullControl,AccessControlType.Deny));
                    security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),PipeAccessRights.FullControl,AccessControlType.Allow));
                    security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null),PipeAccessRights.FullControl,AccessControlType.Allow));
                    security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.InteractiveSid,null),PipeAccessRights.ReadWrite,AccessControlType.Allow));
                    using(var server=new NamedPipeServerStream(pipeName,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous,4096,4096,security)) {
                        lock(gate) { if(stop.WaitOne(0)) return; pipe=server; }
                        var connected=server.BeginWaitForConnection(null,null);
                        using(var ready=connected.AsyncWaitHandle) {
                            if(WaitHandle.WaitAny(new WaitHandle[]{stop,ready})==0) return;
                            server.EndWaitForConnection(connected);
                        }
                        byte[] command=new byte[1];
                        var read=server.ReadAsync(command,0,1);
                        if(!read.Wait(1500) || read.Result!=1) continue;
                        if(command[0]==2) {
                            bool administrator=false;
                            server.RunAsClient(delegate {
                                using(var identity=WindowsIdentity.GetCurrent())
                                    administrator=new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
                            });
                            byte[] tokenBytes=new byte[64]; int received=0;
                            var deadline=DateTime.UtcNow.AddMilliseconds(1500);
                            while(received<tokenBytes.Length) {
                                int remaining=(int)(deadline-DateTime.UtcNow).TotalMilliseconds;
                                if(remaining<=0) break;
                                var tokenRead=server.ReadAsync(tokenBytes,received,tokenBytes.Length-received);
                                if(!tokenRead.Wait(remaining) || tokenRead.Result==0) break;
                                received+=tokenRead.Result;
                            }
                            if(received!=64) continue;
                            QueueUnbind(Encoding.ASCII.GetString(tokenBytes),administrator);
                        }
                        lock(gate) {
                            if(command[0]==1 && unlink!="pending" && DateTime.UtcNow-lastManual>TimeSpan.FromMinutes(1)) {
                                lastManual=DateTime.UtcNow; requestCheck=true; update="checking";
                            }
                        }
                        byte[] result=Encoding.UTF8.GetBytes(Snapshot()+"\n");
                        var write=server.WriteAsync(result,0,result.Length); write.Wait(1500);
                    }
                } catch(Exception error) { if(!stop.WaitOne(0)) { LocalLog.Write("tray_pipe_failed type="+error.GetType().Name); stop.WaitOne(500); } }
                finally { lock(gate) pipe=null; }
            }
        }
    }
}
