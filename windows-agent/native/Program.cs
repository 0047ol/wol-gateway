﻿// .NET Framework 4.x, Windows service, MQTT 3.1.1; no NuGet/runtime bundle.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Security.Cryptography.X509Certificates;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyVersion("1.3.2.0")]
[assembly: System.Reflection.AssemblyFileVersion("1.3.2.0")]
[assembly: System.Reflection.AssemblyTitle("WOL关机服务")]
[assembly: System.Reflection.AssemblyProduct("WOL关机服务")]
[assembly: System.Reflection.AssemblyDescription("WOL关机服务")]

namespace WolPC {
    // Editable installation/storage settings. Rebuild with Build.ps1 after editing.
    // Installation, runtime configuration/logs and uninstallation share this value.
    public static class StorageSettings {
        public const string ExecutableFileName = @"WolPCService.exe";
        public const string InstallDirectory = @"%ProgramFiles%\WolPCService";
        public const string DataFolderName = "WolPCService";
    }

    public static class LocalLog {
        static readonly object gate = new object();
        public static void Write(string code) {
            try {
                lock(gate) {
                    Directory.CreateDirectory(Config.Folder);
                    string path = Path.Combine(Config.Folder,"service.log");
                    if(File.Exists(path) && new FileInfo(path).Length > 131072) File.WriteAllText(path, "", Encoding.UTF8);
                    File.AppendAllText(path, DateTime.UtcNow.ToString("o") + " " + code + Environment.NewLine, Encoding.UTF8);
                }
            } catch {}
        }
    }

    public static class ShutdownResult {
        public static string Classify(bool exited, int exitCode) { return exited && exitCode == 0 ? "submitted" : "failed"; }
    }

    public sealed class Config {
        public string Host = "YOUR_SERVER_HOST";
        public int Port = 8883;
        public bool TLS = true;
        public string Username = "";
        public string Password = "";
        public string AdapterId = "";
        public string Mac = "";
        public static string Folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), StorageSettings.DataFolderName);
        public static string FileName = Path.Combine(Folder, "config.json");
        // Test transport is enabled only by the explicit dry-run harness.
        public static bool AllowTestTransport = false;
        public void Validate() {
            if (!AllowTestTransport) { Host = "YOUR_SERVER_HOST"; Port = 8883; TLS = true; }
            if (String.IsNullOrWhiteSpace(Username) || String.IsNullOrEmpty(Password))
                throw new ArgumentException("Device credentials are missing. Run the application again to repair enrollment.");
            if (String.IsNullOrEmpty(AdapterId) || !System.Text.RegularExpressions.Regex.IsMatch(Mac, "^[0-9a-f]{12}$"))
                throw new ArgumentException("The saved network adapter identity is invalid. Run the application again to repair enrollment.");
        }
        public void Save() {
            Validate();
            Directory.CreateDirectory(Folder);
            Config copy = (Config)MemberwiseClone();
            copy.Password = Convert.ToBase64String(ProtectedData.Protect(Encoding.UTF8.GetBytes(Password), null, DataProtectionScope.LocalMachine));
            string temp = FileName + ".new";
            File.WriteAllText(temp, new JavaScriptSerializer().Serialize(copy), Encoding.UTF8);
            if (File.Exists(FileName)) File.Replace(temp, FileName, null); else File.Move(temp, FileName);
        }
        public static Config Load() {
            Config c = new JavaScriptSerializer().Deserialize<Config>(File.ReadAllText(FileName));
            c.Password = Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(c.Password), null, DataProtectionScope.LocalMachine));
            c.Validate(); return c;
        }
    }

    public static class Trust {
        public static bool Validate(object sender, X509Certificate certificate, X509Chain ignored, SslPolicyErrors errors) {
            if (certificate == null || (errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) return false;
            try {
                string pem = Installer.Resource("WolPC.enroll_ca.pem");
                byte[] raw = Convert.FromBase64String(pem.Replace("-----BEGIN CERTIFICATE-----","").Replace("-----END CERTIFICATE-----",""));
                using(var root = new X509Certificate2(raw))
                using(var chain = new X509Chain()) {
                    chain.ChainPolicy.ExtraStore.Add(root);
                    chain.ChainPolicy.RevocationMode=X509RevocationMode.NoCheck;
                    chain.ChainPolicy.VerificationFlags=X509VerificationFlags.AllowUnknownCertificateAuthority;
                    return chain.Build(new X509Certificate2(certificate)) && chain.ChainElements[chain.ChainElements.Count-1].Certificate.RawData.SequenceEqual(raw);
                }
            } catch { return false; }
        }
    }

    public static class Enrollment {
        public static void Configure() {
            try { var existing=Config.Load(); if(existing.Username==existing.Mac) { existing.Save(); return; } } catch {}
            var candidates=NetworkInterface.GetAllNetworkInterfaces().Where(n=>(n.NetworkInterfaceType==NetworkInterfaceType.Ethernet || n.NetworkInterfaceType==NetworkInterfaceType.Wireless80211) && n.GetPhysicalAddress().GetAddressBytes().Length==6).ToList();
            int best=AdapterChoice.Best(candidates,"YOUR_SERVER_HOST",8883);
            if(best<0) throw new NetworkInformationException();
            var adapter=candidates[best]; string mac=adapter.GetPhysicalAddress().ToString().ToLowerInvariant();
            ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
            var request=(HttpWebRequest)WebRequest.Create("https://YOUR_SERVER_HOST:8443/api/enroll/pc");
            request.ServerCertificateValidationCallback=Trust.Validate;
            request.Method="POST"; request.ContentType="application/json"; request.Timeout=20000;
            byte[] body=Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(new {mac=mac,version=Maintenance.Version}));
            request.ContentLength=body.Length;
            using(var stream=request.GetRequestStream()) stream.Write(body,0,body.Length);
            Dictionary<string,object> result;
            using(var response=request.GetResponse())
            using(var reader=new StreamReader(response.GetResponseStream())) result=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
            if((string)result["username"]!=mac || (string)result["host"]!="YOUR_SERVER_HOST" || Convert.ToInt32(result["port"])!=8883) throw new ArgumentException("The enrollment server returned a different device or endpoint.");
            new Config {Host=(string)result["host"],Port=8883,TLS=true,Username=mac,Password=(string)result["password"],Mac=mac,AdapterId=adapter.Id}.Save();
        }
    }

    public static class Wire {
        public static byte[] Utf(string value) {
            byte[] b = Encoding.UTF8.GetBytes(value);
            if (b.Length > 65535) throw new InvalidDataException("MQTT string too long");
            return new byte[] {(byte)(b.Length >> 8), (byte)b.Length}.Concat(b).ToArray();
        }
        public static byte[] Packet(int type, byte[] body) {
            List<byte> bytes = new List<byte>(); bytes.Add((byte)type);
            int n = body.Length;
            do { int x = n % 128; n /= 128; bytes.Add((byte)(x | (n > 0 ? 128 : 0))); } while(n > 0);
            bytes.AddRange(body); return bytes.ToArray();
        }
        public static byte[] Read(Stream stream, out int header) {
            header = stream.ReadByte(); if (header < 0) throw new EndOfStreamException();
            int length = 0, scale = 1, count = 0, b;
            do {
                if (++count > 4) throw new InvalidDataException("Invalid MQTT length");
                b = stream.ReadByte(); if (b < 0) throw new EndOfStreamException();
                length += (b & 127) * scale; scale *= 128;
                if (length > 16384) throw new InvalidDataException("MQTT packet too large");
            } while ((b & 128) != 0);
            byte[] body = new byte[length]; int read = 0;
            while (read < length) { int n = stream.Read(body, read, length - read); if(n == 0) throw new EndOfStreamException(); read += n; }
            return body;
        }
    }

    public sealed class CommandGate {
        public string RejectionReason { get; private set; }
        readonly string session;
        readonly HashSet<string> seen = new HashSet<string>();
        public CommandGate(string value) { session = value; }
        public bool Accept(string payload, bool retained, long now, out string id) {
            id = "";
            RejectionReason = "invalid_payload";
            if (retained || payload.Length > 4096) { RejectionReason = retained ? "retained" : "invalid_payload"; return false; }
            try {
                var v = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(payload);
                if ((string)v["type"] != "shutdown") return false;
                if ((string)v["session"] != session) { RejectionReason="wrong_session"; return false; }
                id = (string)v["id"];
                long expiry = Convert.ToInt64(v["expires_at"]);
                if (!System.Text.RegularExpressions.Regex.IsMatch(id, "^[0-9a-f]{32}$")) return false;
                if (expiry < now || expiry > now + 60) { RejectionReason="clock_or_expired"; return false; }
                if (seen.Count >= 128 || !seen.Add(id)) { RejectionReason="duplicate_or_limit"; return false; }
                RejectionReason="";
                return true;
            } catch { return false; }
        }
    }

    public sealed class Agent {
        readonly Config config;
        readonly bool allowShutdown;
        readonly ManualResetEvent stop = new ManualResetEvent(false);
        readonly AutoResetEvent networkChanged = new AutoResetEvent(false);
        readonly object sendLock = new object();
        readonly Random retryRandom = new Random();
        bool subscribedThisAttempt;
        string connectionPhase = "connect";
        TcpClient tcp;
        Stream stream;
        public Agent(Config c, bool powerEnabled = false) { config = c; allowShutdown = powerEnabled; }
        public void Stop() { stop.Set(); try { if(tcp != null) tcp.Close(); } catch {} }
        static long Now() { return (long)(DateTime.UtcNow - new DateTime(1970,1,1)).TotalSeconds; }
        void Send(int header, byte[] body) {
            byte[] packet = Wire.Packet(header, body);
            lock(sendLock) { stream.Write(packet, 0, packet.Length); stream.Flush(); }
        }
        void Publish(string topic, string value, bool retain) {
            Send(retain ? 0x31 : 0x30, Wire.Utf(topic).Concat(Encoding.UTF8.GetBytes(value)).ToArray());
        }
        string Status(string session, string state) {
            var adapter = NetworkInterface.GetAllNetworkInterfaces().FirstOrDefault(n => n.Id == config.AdapterId);
            string ip = adapter == null ? "0.0.0.0" : adapter.GetIPProperties().UnicastAddresses
                .Where(a => a.Address.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(a.Address))
                .Select(a => a.Address.ToString()).FirstOrDefault() ?? "0.0.0.0";
            return new JavaScriptSerializer().Serialize(new {name=Environment.MachineName, ip=ip, mac=config.Mac, session=session, state=state, version=Maintenance.Version});
        }
        void ConnectAndRun() {
            subscribedThisAttempt = false;
            connectionPhase = "connect";
            string session = Guid.NewGuid().ToString("N"), root = "wol/pc/" + config.Mac;
            CommandGate commands = new CommandGate(session);
            tcp = new TcpClient();
            IAsyncResult pending = tcp.BeginConnect(config.Host, config.Port, null, null);
            try {
                int connected = WaitHandle.WaitAny(new WaitHandle[] {stop, pending.AsyncWaitHandle}, 10000);
                if (connected != 1) throw new IOException("MQTT connection cancelled or timed out");
                tcp.EndConnect(pending);
            } finally { pending.AsyncWaitHandle.Close(); }
            tcp.ReceiveTimeout = 10000; tcp.SendTimeout = 10000;
            stream = tcp.GetStream();
            if (config.TLS) {
                connectionPhase = "tls";
                var tls = new SslStream(stream, false, Trust.Validate);
                stream = tls;
                IAsyncResult auth = tls.BeginAuthenticateAsClient(config.Host, null, System.Security.Authentication.SslProtocols.Tls12, true, null, null);
                try {
                    if(WaitHandle.WaitAny(new WaitHandle[] {stop, auth.AsyncWaitHandle}, 10000) != 1) throw new IOException("TLS handshake cancelled or timed out");
                    tls.EndAuthenticateAsClient(auth);
                } finally { auth.AsyncWaitHandle.Close(); }
            }
            int flags = 2 | 4 | 32; // clean session, will present, retained offline will
            if(config.Username.Length > 0 || config.Password.Length > 0) flags |= 128;
            if(config.Password.Length > 0) flags |= 64;
            var body = new List<byte>(Wire.Utf("MQTT"));
            body.AddRange(new byte[] {4, (byte)flags, 0, 30});
            body.AddRange(Wire.Utf("wolpc-" + config.Mac));
            body.AddRange(Wire.Utf(root + "/status")); body.AddRange(Wire.Utf(Status(session, "offline")));
            if((flags & 128) != 0) body.AddRange(Wire.Utf(config.Username));
            if((flags & 64) != 0) body.AddRange(Wire.Utf(config.Password));
            connectionPhase = "connack";
            Send(0x10, body.ToArray());
            int header; byte[] reply = Wire.Read(stream, out header);
            if(header != 0x20 || reply.Length != 2 || reply[1] != 0) throw new IOException("MQTT connection rejected");
            connectionPhase = "subscribe";
            Send(0x82, new byte[] {0,1}.Concat(Wire.Utf(root + "/command")).Concat(new byte[] {0}).ToArray());
            bool subscribed = false;
            var clock = Stopwatch.StartNew();
            long heartbeatAt = 20000, pingDeadline = 0;
            Task<Tuple<int,byte[]>> readTask = null;
            try {
                while(!stop.WaitOne(0)) {
                    if(pingDeadline != 0 && clock.ElapsedMilliseconds >= pingDeadline) throw new IOException("MQTT PINGRESP timed out");
                    if(subscribed && pingDeadline == 0 && clock.ElapsedMilliseconds >= heartbeatAt) {
                        Publish(root + "/status", Status(session,"online"),true);
                        Send(0xC0,new byte[0]);
                        pingDeadline = clock.ElapsedMilliseconds + 10000;
                        heartbeatAt = clock.ElapsedMilliseconds + 20000;
                    }
                    // One reader per connection; all writes stay on this worker. No timer
                    // from a previous connection can publish through a newer session.
                    if(readTask == null) {
                        Stream input = stream;
                        readTask = Task.Factory.StartNew(() => {
                            int h; byte[] p = Wire.Read(input, out h);
                            return Tuple.Create(h,p);
                        });
                    }
                    if(!readTask.Wait(250)) {
                        continue;
                    }
                    header = readTask.Result.Item1;
                    byte[] packet = readTask.Result.Item2;
                    readTask = null;
                    if (header == 0x90) {
                        if(packet.Length != 3 || packet[0] != 0 || packet[1] != 1 || packet[2] > 1) throw new IOException("MQTT subscription rejected");
                        subscribed = subscribedThisAttempt = true;
                        connectionPhase = "online";
                        stream.ReadTimeout = 45000;
                        heartbeatAt = clock.ElapsedMilliseconds + 20000;
                        Publish(root + "/status", Status(session, "online"), true);
                        LocalLog.Write("mqtt_subscribed version="+Maintenance.Version);
                    } else if(header == 0xD0) {
                        if(packet.Length != 0) throw new InvalidDataException("Invalid PINGRESP");
                        pingDeadline = 0;
                    } else if ((header >> 4) == 3 && subscribed) {
                        if(packet.Length < 2) throw new InvalidDataException();
                        int topicLength = (packet[0] << 8) | packet[1], offset = topicLength + 2, qos = (header >> 1) & 3;
                        if(offset > packet.Length || qos > 1) throw new InvalidDataException();
                        string topic = Encoding.UTF8.GetString(packet, 2, topicLength);
                        if(qos == 1) { if(offset + 2 > packet.Length) throw new InvalidDataException(); Send(0x40, new byte[] {packet[offset], packet[offset+1]}); offset += 2; }
                        string id;
                        if(topic != root + "/command") continue;
                        if(!commands.Accept(Encoding.UTF8.GetString(packet, offset, packet.Length-offset), (header & 1) != 0, Now(), out id)) {
                            LocalLog.Write("command_rejected reason=" + commands.RejectionReason);
                            if(commands.RejectionReason=="clock_or_expired") Publish(root + "/ack",new JavaScriptSerializer().Serialize(new {id=id,session=session,status="rejected",reason="clock_or_expired"}),false);
                            continue;
                        }
                        LocalLog.Write("command_accepted");
                        Publish(root + "/ack", new JavaScriptSerializer().Serialize(new {id=id, session=session, status="accepted"}), false);
                        if (!allowShutdown) { Publish(root + "/ack", new JavaScriptSerializer().Serialize(new {id=id, session=session, status="dry_run"}), false); continue; }
                        try {
                            using(var process=Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "shutdown.exe"), "/s /t 0") {UseShellExecute=false, CreateNoWindow=true})) {
                                bool exited=process.WaitForExit(10000);
                                int exitCode=exited ? process.ExitCode : -1;
                                string outcome=ShutdownResult.Classify(exited,exitCode);
                                LocalLog.Write("shutdown_"+outcome+" exit_code="+exitCode);
                                Publish(root + "/ack", new JavaScriptSerializer().Serialize(new {id=id, session=session, status=outcome, exit_code=exitCode}), false);
                            }
                        } catch (Exception error) {
                            LocalLog.Write("shutdown_exception type="+error.GetType().Name+" hresult="+error.HResult);
                            try { Publish(root + "/ack", new JavaScriptSerializer().Serialize(new {id=id, session=session, status="failed"}), false); } catch {}
                        }
                    }
                }
            } finally {
                tcp.Close();
                // Closing the socket unblocks a pending read; observe its exception.
                if(readTask != null) { try { readTask.Wait(2000); } catch {} }
            }
        }
        public static int RetryDelay(int failureCount) {
            return 1000 * (int)Math.Min(30, Math.Pow(2, Math.Min(5, Math.Max(0,failureCount - 1))));
        }
        void NetworkChanged(object sender, EventArgs args) { networkChanged.Set(); }
        public void Run() {
            int failures = 0;
            NetworkChange.NetworkAddressChanged += NetworkChanged;
            try {
                while(!stop.WaitOne(0)) {
                    try { ConnectAndRun(); }
                    catch (Exception error) {
                        while(error.InnerException != null) error = error.InnerException;
                        if(!stop.WaitOne(0)) LocalLog.Write("connection_retry stage="+connectionPhase+" type="+error.GetType().Name+" hresult="+error.HResult);
                    }
                    finally { try { if(stream != null) stream.Dispose(); if(tcp != null) tcp.Close(); } catch {} }
                    if(stop.WaitOne(0)) break;
                    failures = subscribedThisAttempt ? 1 : Math.Min(6,failures + 1);
                    int delay = RetryDelay(failures) + retryRandom.Next(251);
                    LocalLog.Write("connection_backoff delay_ms="+delay);
                    WaitHandle.WaitAny(new WaitHandle[] {stop,networkChanged}, delay);
                }
            } finally {
                NetworkChange.NetworkAddressChanged -= NetworkChanged;
            }
        }
    }

    public sealed class PCService : ServiceBase {
        Agent agent; Thread worker;
        Maintenance maintenance;
        readonly ManualResetEvent stopping = new ManualResetEvent(false);
        public PCService() { ServiceName="WolPCService"; CanStop=true; AutoLog=false; }
        protected override void OnStart(string[] args) {
            Platform.SecureDirectory(Config.Folder,false);
            LocalLog.Write("service_start version="+Maintenance.Version);
            maintenance=new Maintenance(); maintenance.Start();
            worker = new Thread(() => {
                int failures=0;
                while(!stopping.WaitOne(0)) {
                    try {
                        Enrollment.Configure();
                        try { File.Delete(Path.Combine(Config.Folder,"startup-error.txt")); } catch {}
                        var next = new Agent(Config.Load(), true);
                        agent=next;
                        if(stopping.WaitOne(0)) { next.Stop(); break; }
                        next.Run();
                    } catch(Exception error) {
                        LocalLog.Write("startup_retry type="+error.GetType().Name+" hresult="+error.HResult);
                        try { File.WriteAllText(Path.Combine(Config.Folder,"startup-error.txt"),"Service startup / enrollment failed\r\n"+ErrorDetails.Describe(error),Encoding.UTF8); } catch {}
                    }
                    if(stopping.WaitOne(Agent.RetryDelay(++failures))) break;
                }
            });
            worker.IsBackground=true; worker.Start();
        }
        protected override void OnStop() { stopping.Set(); if(maintenance!=null) maintenance.Stop(); if(agent != null) agent.Stop(); if(worker != null) worker.Join(12000); }
    }

    public static class ErrorDetails {
        public static string FailureCode(Exception error) {
            // Method names identify the failing operation without logging paths,
            // exception messages, credentials, command arguments or local values.
            var frames=new System.Diagnostics.StackTrace(error,false).GetFrames();
            string site=frames==null?"unknown":String.Join(">",frames.Select(frame=>frame.GetMethod()).Where(method=>method!=null && method.DeclaringType!=null && method.DeclaringType.Namespace=="WolPC").Take(6).Select(method=>method.DeclaringType.Name+"."+method.Name));
            return "type="+error.GetType().Name+" hresult="+error.HResult+" site="+site;
        }
        public static string Describe(Exception error) {
            var web=error as WebException;
            if(error is NetworkInformationException) return "No connected Ethernet or Wi-Fi adapter has a usable IPv4 address. Connect the network; the service will retry automatically. No configuration editing is required.";
            if(web != null) {
                var response=web.Response as HttpWebResponse;
                if(response != null && response.StatusCode==HttpStatusCode.Forbidden) return "This PC MAC address is not registered. Add the current adapter MAC to the dashboard device list; the service will retry automatically.";
                return "The enrollment server could not be reached or verified. Check network connectivity and system time. Status: "+web.Status+". The service will retry automatically.";
            }
            return error.GetType().Name+": "+error.Message+" (HRESULT "+error.HResult+")";
        }
        public static void Show(string stage, string detail) {
            MessageBox.Show(detail,stage,MessageBoxButtons.OK,MessageBoxIcon.Error);
        }
    }

    public static class AdapterChoice {
        public static int Rank(bool up, bool ipv4, bool wired, bool gateway, bool routed, bool virtualAdapter) {
            if (!up || !ipv4) return 0;
            if (virtualAdapter) return 10;
            if (wired && gateway) return routed ? 1000 : 900;
            if (routed) return 700;
            if (gateway) return 600;
            return wired ? 400 : 300;
        }
        public static int Best(IList<NetworkInterface> candidates, string host, int port) {
            string routeIP = "";
            try {
                // UDP Connect resolves the local outgoing interface without sending a packet.
                using (var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp)) {
                    socket.Connect(host, port);
                    routeIP = ((IPEndPoint)socket.LocalEndPoint).Address.ToString();
                }
            } catch {}
            int best = -1, bestRank = -1;
            for (int i = 0; i < candidates.Count; i++) {
                try {
                    var n = candidates[i]; var properties = n.GetIPProperties();
                    var addresses = properties.UnicastAddresses.Where(a => a.Address.AddressFamily == AddressFamily.InterNetwork && !IPAddress.IsLoopback(a.Address) && !a.Address.ToString().StartsWith("169.254.")).ToList();
                    bool gateway = properties.GatewayAddresses.Any(g => g.Address.AddressFamily == AddressFamily.InterNetwork && !g.Address.Equals(IPAddress.Any));
                    bool virtualAdapter = System.Text.RegularExpressions.Regex.IsMatch(n.Name + " " + n.Description, "virtual|hyper-v|vethernet|vmware|vpn|loopback|tap-|tun", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
                    int rank = Rank(n.OperationalStatus == OperationalStatus.Up, addresses.Count > 0,
                        n.NetworkInterfaceType == NetworkInterfaceType.Ethernet, gateway,
                        addresses.Any(a => a.Address.ToString() == routeIP), virtualAdapter);
                    if (rank > 0 && rank > bestRank) { best = i; bestRank = rank; }
                } catch {}
            }
            return best;
        }
    }

    public static class Program {
        [STAThread]
        public static int Main(string[] args) {
            try { return MainCore(args); }
            catch(Exception error) {
                LocalLog.Write("operation_failed version="+Maintenance.Version+" "+ErrorDetails.FailureCode(error));
                if(args.Contains("--apply-update")) try {
                    string result=Path.Combine(Config.Folder,@"updates\last-result.txt");
                    if(!File.Exists(result) || !File.ReadAllText(result).StartsWith("rollback_failed:")) File.WriteAllText(result,"failed:"+error.GetType().Name);
                } catch {}
                if(Environment.UserInteractive && !args.Contains("--apply-update") && !args.Contains("--worker-uninstall"))
                    ErrorDetails.Show("WOL关机服务",error.Message);
                return 1;
            }
        }
        static int MainCore(string[] args) {
            if(!Installer.RuntimeReady()) {
                if(Environment.UserInteractive) MessageBox.Show("请先安装 .NET Framework 4.8，再运行WOL关机服务。","WOL关机服务",MessageBoxButtons.OK,MessageBoxIcon.Information);
                return 3;
            }
            int operation=Installer.Handle(args); if(operation>=0) return operation;
            if(args.Length==1 && args[0]=="--tray-host" && Environment.UserInteractive) { Desktop.Run(); return 0; }
            if(args.Contains("--configure")) {
                if(!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator)) return 2;
                try { Enrollment.Configure(); return 0; }
                catch (Exception error) {
                    LocalLog.Write("enrollment_failed type="+error.GetType().Name+" hresult="+error.HResult);
                    var web=error as WebException;
                    if(error is NetworkInformationException || (web!=null && web.Response==null && web.Status!=WebExceptionStatus.TrustFailure && web.Status!=WebExceptionStatus.SecureChannelFailure)) {
                        LocalLog.Write("enrollment_deferred automatic_retry=true"); return 0;
                    }
                    try { Directory.CreateDirectory(Config.Folder); File.WriteAllText(Path.Combine(Config.Folder,"enrollment-error.txt"),ErrorDetails.Describe(error),Encoding.UTF8); } catch {}
                    return 1;
                }
            }
            if(args.Contains("--self-test")) return SelfTest();
            if(args.Contains("--tls-test")) {
                try {
                    using(var tcp=new TcpClient("YOUR_SERVER_HOST",8883))
                    using(var tls=new SslStream(tcp.GetStream(),false,Trust.Validate))
                        tls.AuthenticateAsClient("YOUR_SERVER_HOST",null,System.Security.Authentication.SslProtocols.Tls12,true);
                    ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
                    var probe=(HttpWebRequest)WebRequest.Create("https://YOUR_SERVER_HOST:8443/api/enroll/pc");
                    probe.Proxy=null; probe.Timeout=12000; probe.ServerCertificateValidationCallback=Trust.Validate;
                    probe.Method="POST";probe.ContentType="application/json";
                    byte[] data=Encoding.UTF8.GetBytes("{\"mac\":\"000000000000\",\"version\":\"1.1.0\"}");
                    probe.ContentLength=data.Length;
                    using(var requestStream=probe.GetRequestStream()) requestStream.Write(data,0,data.Length);
                    try { using(var result=probe.GetResponse()) {} return 3; }
                    catch(WebException error) { return error.Response is HttpWebResponse && ((HttpWebResponse)error.Response).StatusCode==HttpStatusCode.Forbidden ? 0 : 2; }
                } catch { return 1; }
            }
            if(args.Contains("--protocol-test")) {
                // Test transport only: shutdown is disabled even for a valid command.
                Config.AllowTestTransport=true; Config.FileName=args[1]; Config.Folder=Path.GetDirectoryName(Path.GetFullPath(Config.FileName));
                var agent=new Agent(Config.Load()); var thread=new Thread(agent.Run); thread.Start(); Thread.Sleep(args.Length > 2 ? Math.Max(1, Math.Min(60, Int32.Parse(args[2]))) * 1000 : 4000); agent.Stop(); thread.Join(12000); return 0;
            }
            if(Environment.UserInteractive) {
                string installed=Path.Combine(Environment.ExpandEnvironmentVariables(StorageSettings.InstallDirectory),StorageSettings.ExecutableFileName);
                if(args.Contains("--tray") || String.Equals(Path.GetFullPath(installed),Path.GetFullPath(Application.ExecutablePath),StringComparison.OrdinalIgnoreCase)) {
                    Desktop.Launch(); return 0;
                }
                try {
                    int result=Platform.Administrator?Installer.Install(false,Application.ExecutablePath):Platform.Elevated(new[]{"--install","--no-tray"});
                    if(result==0) Platform.StartTray();
                    return result;
                } catch(System.ComponentModel.Win32Exception error) { ErrorDetails.Show("Installation", error.NativeErrorCode==1223 ? "Administrator approval was cancelled. No installation was started." : ErrorDetails.Describe(error)); return 1; }
            }
            ServiceBase.Run(new PCService()); return 0;
        }
        static int SelfTest() {
            if(!Installer.Resource("WolPC.enroll_ca.pem").Contains("BEGIN CERTIFICATE")) return 30;
            if(Agent.RetryDelay(1)!=1000 || Agent.RetryDelay(2)!=2000 || Agent.RetryDelay(6)!=30000 || Agent.RetryDelay(100)!=30000) return 16;
            string session=new string('a',32), id=new string('b',32);
            Func<string,long,string> command=(s,expiry)=>new JavaScriptSerializer().Serialize(new {id=id,type="shutdown",session=s,expires_at=expiry});
            string ignored; var gate=new CommandGate(session);
            if(gate.Accept(command(session,130),true,100,out ignored)) return 1;
            if(gate.Accept(command(new string('c',32),130),false,100,out ignored)) return 2;
            if(gate.Accept(command(session,99),false,100,out ignored)) return 3;
            if(gate.Accept(command(session,200),false,100,out ignored)) return 4;
            if(gate.Accept("{broken",false,100,out ignored)) return 5;
            if(!gate.Accept(command(session,130),false,100,out ignored)) return 6;
            if(gate.Accept(command(session,130),false,100,out ignored)) return 7;
            int header;
            byte[] large=Enumerable.Range(0,500).Select(i=>(byte)i).ToArray();
            if(!Wire.Read(new MemoryStream(Wire.Packet(0x30,large)),out header).SequenceEqual(large) || header!=0x30) return 8;
            try { Wire.Read(new MemoryStream(new byte[]{0x30,0x80,0x80,0x80,0x80,0}),out header); return 9; } catch(InvalidDataException) {}
            try { Wire.Read(new MemoryStream(new byte[]{0x30,2,1}),out header); return 10; } catch(EndOfStreamException) {}
            // Prefer a live routed wired adapter over Wi-Fi, virtual and disconnected adapters.
            int wired=AdapterChoice.Rank(true,true,true,true,true,false);
            int wifi=AdapterChoice.Rank(true,true,false,true,true,false);
            int virtualNic=AdapterChoice.Rank(true,true,true,true,true,true);
            if(wired <= wifi || virtualNic >= wifi || AdapterChoice.Rank(false,true,true,true,true,false) != 0) return 11;
            if(AdapterChoice.Rank(true,false,true,true,true,false) != 0 || wifi <= AdapterChoice.Rank(true,true,true,false,false,false)) return 12;
            if(new Config().Host != "YOUR_SERVER_HOST" || new Config().Port != 8883 || !new Config().TLS) return 13;
            if(ShutdownResult.Classify(true,0)!="submitted" || ShutdownResult.Classify(true,5)!="failed" || ShutdownResult.Classify(false,0)!="failed") return 14;
            var rejected=new CommandGate(session);
            if(rejected.Accept(command(session,10),false,100,out ignored) || rejected.RejectionReason!="clock_or_expired") return 15;
            return 0;
        }
    }
}
