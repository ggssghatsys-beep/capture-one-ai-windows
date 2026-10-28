using System;
using System.IO;
using System.IO.Pipes;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Reflection;
using System.Runtime.Loader;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Generic;
using System.Windows.Forms;

internal class StartupHook
{
    public static void Initialize()
    {
        try { C1InternalBootstrap.Start(); }
        catch (Exception e) { C1InternalBootstrap.Log("startup refused: " + e.Message); }
    }
}
internal static class C1InternalBootstrap
{
    static readonly string Root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CaptureOneAI");
    static readonly Dictionary<string, (string Hash,string Result)> Receipts = new();
    static string token = "";
    static string engineFile = "";
    static MethodInfo? engine;
    public static void Log(string message)
    {
        try { Directory.CreateDirectory(Root); File.AppendAllText(Path.Combine(Root,"internal-bridge.log"), DateTime.UtcNow.ToString("O")+" "+message+Environment.NewLine); } catch { }
    }
    public static void Start()
    {
        string exe=Environment.ProcessPath ?? "";
        if (!Path.GetFileName(exe).Equals("CaptureOne.exe",StringComparison.OrdinalIgnoreCase)) return;
        // Do not propagate our hook into C1's helper processes.
        string own=typeof(StartupHook).Assembly.Location;
        string hooks=Environment.GetEnvironmentVariable("DOTNET_STARTUP_HOOKS") ?? "";
        Environment.SetEnvironmentVariable("DOTNET_STARTUP_HOOKS",string.Join(";",Array.FindAll(hooks.Split(';'),p=>!p.Equals(own,StringComparison.OrdinalIgnoreCase))));
        token=File.ReadAllText(Path.Combine(Root,"internal-token.txt")).Trim();
        if(token.Length!=64) throw new InvalidOperationException("invalid internal bridge token");
        Task.Run(Serve);
        Log("hook loaded pid="+Environment.ProcessId+" assembly="+own);
    }
    static object Envelope(string kind, object? result=null) => new {protocol="capture-one-ai",version=1,kind,owner_pid=Environment.ProcessId,result};
    static async Task Write(Stream s,object data)
    {
        byte[] b=JsonSerializer.SerializeToUtf8Bytes(data); byte[] n=BitConverter.GetBytes(b.Length);
        using var timeout=new CancellationTokenSource(5000);
        await s.WriteAsync(n,timeout.Token);await s.WriteAsync(b,timeout.Token);await s.FlushAsync(timeout.Token);
    }
    static async Task<JsonDocument> Read(Stream s)
    {
        using var timeout=new CancellationTokenSource(5000);
        byte[] n=new byte[4];await s.ReadExactlyAsync(n,timeout.Token);int len=BitConverter.ToInt32(n);
        if(len<2||len>1048576)throw new InvalidDataException("frame size");
        byte[] b=new byte[len];await s.ReadExactlyAsync(b,timeout.Token);return JsonDocument.Parse(b);
    }
    static object Error(string code,string message) => new {status="refused",error=new {code,message},dispatched=false};
    static async Task Serve()
    {
        string name="CaptureOneAI.Internal."+WindowsIdentity.GetCurrent().User!.Value;
        while(true)
        {
            try
            {
                using var pipe=new NamedPipeServerStream(name,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous|PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync();
                using var hello=await Read(pipe);
                if(hello.RootElement.GetProperty("kind").GetString()!="hello"||hello.RootElement.GetProperty("protocol").GetString()!="capture-one-ai"||hello.RootElement.GetProperty("version").GetInt32()!=1) continue;
                await Write(pipe,Envelope("hello"));
                using var frame=await Read(pipe);var request=frame.RootElement.GetProperty("request");
                string supplied=request.TryGetProperty("token",out var t)?t.GetString()??"":"";
                if(!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(supplied),Encoding.UTF8.GetBytes(token)))
                {await Write(pipe,Envelope("result",Error("AUTH_REQUIRED","internal bridge token mismatch")));continue;}
                string command=request.GetProperty("command").GetString()??"";
                if(command=="status")
                {await Write(pipe,Envelope("result",new {status="ok",pid=Environment.ProcessId,started_at=Process.GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc().ToString(),backend="c1_internal_model",engine_loaded=engine!=null}));continue;}
                if(!request.TryGetProperty("expected_instance",out var instance)||instance.GetProperty("pid").GetInt32()!=Environment.ProcessId||instance.GetProperty("started_at").GetString()!=Process.GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc().ToString())
                {await Write(pipe,Envelope("result",Error("INSTANCE_CHANGED","request must name this C1 process and start time")));continue;}
                if(command=="context"||command=="read"||command=="inspect"||command=="viewer")
                {await Write(pipe,Envelope("result",JsonSerializer.Deserialize<object>(await OnUi(request.GetRawText()))));continue;}
                string id=request.GetProperty("request_id").GetString()??"";
                if(id.Length==0||id.Length>128){await Write(pipe,Envelope("result",Error("REQUEST_ID_REQUIRED","nonempty request id required")));continue;}
                string hash=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(request.GetRawText())));
                if(Receipts.TryGetValue(id,out var stored))
                {
                    await Write(pipe,Envelope("result",stored.Hash==hash?JsonSerializer.Deserialize<object>(stored.Result):Error("REQUEST_CONFLICT","request id names different content")));continue;
                }
                string input=request.GetRawText();
                Receipts[id]=(hash,JsonSerializer.Serialize(new {status="outcome_unknown",dispatched=true,replay_allowed=false}));
                string output=await OnUi(input);
                // Never replay a submitted request; retain its exact result for this C1 process.
                Receipts[id]=(hash,output);
                await Write(pipe,Envelope("result",JsonSerializer.Deserialize<object>(output)));
            }
            catch(Exception e){Log("pipe: "+e.GetType().Name+" "+e.Message);}
        }
    }
    static async Task<string> OnUi(string request)
    {
        Form? form=null;
        IntPtr main=Process.GetCurrentProcess().MainWindowHandle;
        foreach(Form f in Application.OpenForms)if(f.IsHandleCreated&&!f.IsDisposed&&f.Handle==main){form=f;break;}
        if(form==null)return JsonSerializer.Serialize(Error("C1_NOT_READY","main form not ready"));
        var completion=new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        int phase=0;
        long deadline=Environment.TickCount64+10000;
        form.BeginInvoke((Action)(()=>
        {
            if(Environment.TickCount64>deadline||Interlocked.CompareExchange(ref phase,1,0)!=0){completion.TrySetResult(JsonSerializer.Serialize(Error("UI_DEADLINE","request expired before model dispatch")));return;}
            bool enteredEngine=false;
            try
            {
                using var config=JsonDocument.Parse(File.ReadAllText(Path.Combine(Root,"internal-engine.json")));
                string path=config.RootElement.GetProperty("path").GetString()!;
                string hash=config.RootElement.GetProperty("sha256").GetString()!;
                string allowed=Path.GetFullPath(Path.Combine(Path.GetDirectoryName(typeof(StartupHook).Assembly.Location)!,"..","..","bin"))+Path.DirectorySeparatorChar;
                if(!Path.GetFullPath(path).StartsWith(allowed,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("engine must be in the local internal/bin folder");
                if(path!=engineFile)
                {
                    using var fs=File.OpenRead(path);
                    if(!Convert.ToHexString(SHA256.HashData(fs)).Equals(hash,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("engine hash mismatch");
                    var assembly=AssemblyLoadContext.Default.LoadFromAssemblyPath(path);
                    engine=assembly.GetType("C1NativeEngine",true)!.GetMethod("Handle",BindingFlags.Static|BindingFlags.Public)!;
                    engineFile=path;Log("engine loaded "+path);
                }
                enteredEngine=true;
                completion.TrySetResult((string)engine!.Invoke(null,new object[]{request})!);
            }
            catch(Exception e){completion.TrySetResult(JsonSerializer.Serialize(enteredEngine?(object)new {status="outcome_unknown",error=new {code="INTERNAL_MODEL_ERROR",message=(e.InnerException??e).ToString()},dispatched=true,replay_allowed=false}:Error("INTERNAL_MODEL_ERROR",(e.InnerException??e).ToString())));}
        }));
        try {return await completion.Task.WaitAsync(TimeSpan.FromSeconds(30));}
        catch(TimeoutException)
        {
            if(Interlocked.CompareExchange(ref phase,2,0)==0)return JsonSerializer.Serialize(Error("UI_DEADLINE","request expired before model dispatch"));
            return JsonSerializer.Serialize(new {status="outcome_unknown",error=new {code="UI_TIMEOUT",message="native call has not returned; inspect state before another edit"},dispatched=true,replay_allowed=false});
        }
    }
}
