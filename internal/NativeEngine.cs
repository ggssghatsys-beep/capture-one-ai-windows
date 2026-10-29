using System;
using System.IO;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text.Json;
using System.Globalization;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Forms;
using System.Windows.Forms.Integration;

public static class C1NativeEngine
{
    sealed class RangeWrite {public object List=null!;public object? Added;public int RemovedIndex=-1;public int Count;}
    static readonly BindingFlags Flags=BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance;
    static object? cachedDocument;
    static object? maskManager,maskDocument;
    sealed class PreviewJob
    {
        public string Id=Guid.NewGuid().ToString("N"),Key="",Revision="",Kind="";
        public object Geometry=null!;
        public Task<object> Work=null!;
    }
    static PreviewJob? previewJob,pixelPreviewJob;
    static object? Member(object? o,string name)
    {
        if(o==null)return null;
        for(Type? t=o.GetType();t!=null;t=t.BaseType)
        {
            var p=t.GetProperty(name,Flags|BindingFlags.DeclaredOnly);
            if(p!=null&&p.GetIndexParameters().Length==0)return p.GetValue(o);
            var f=t.GetField(name,Flags|BindingFlags.DeclaredOnly);if(f!=null)return f.GetValue(o);
        }
        foreach(var i in o.GetType().GetInterfaces())
        {var p=i.GetProperty(name);if(p!=null&&p.GetIndexParameters().Length==0)return p.GetValue(o);}
        return null;
    }
    static IEnumerable<System.Windows.Forms.Control> Controls(System.Windows.Forms.Control c)
    {yield return c;foreach(System.Windows.Forms.Control n in c.Controls)foreach(var x in Controls(n))yield return x;}
    static IEnumerable<DependencyObject> Visuals(DependencyObject root)
    {
        var q=new Queue<DependencyObject>();q.Enqueue(root);int count=0;
        while(q.Count>0&&count++<12000)
        {
            var x=q.Dequeue();yield return x;
            int n=0;try{n=VisualTreeHelper.GetChildrenCount(x);}catch{}
            for(int i=0;i<n;i++)q.Enqueue(VisualTreeHelper.GetChild(x,i));
        }
    }
    static List<object> Models()
    {
        var models=new List<object>();var seen=new HashSet<object>(ReferenceEqualityComparer.Instance);
        foreach(Form f in System.Windows.Forms.Application.OpenForms)
        if(f.Handle==System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle)
        foreach(var c in Controls(f))
        if(c is ElementHost h&&h.Child!=null)
        foreach(var node in Visuals(h.Child))
        if(node is FrameworkElement fe&&fe.DataContext!=null&&seen.Add(fe.DataContext)) models.Add(fe.DataContext);
        return models;
    }
    static string Name(object o)=>o.GetType().FullName??o.GetType().Name;
    static PropertyInfo Property(object o,string name)
    {
        for(Type? t=o.GetType();t!=null;t=t.BaseType){var p=t.GetProperty(name,Flags|BindingFlags.DeclaredOnly);if(p!=null)return p;}
        throw new InvalidOperationException("native property unavailable: "+name);
    }
    static object? Invoke(object o,string method,params object?[] args)
    {
        var candidates=o.GetType().GetMethods(Flags).Where(m=>m.Name==method&&m.GetParameters().Length==args.Length).ToArray();
        var match=candidates.Single(m=>m.GetParameters().Zip(args).All(pair=>pair.Second==null||pair.First.ParameterType.IsInstanceOfType(pair.Second)));
        return match.Invoke(o,args);
    }
    static IEnumerable<object> Items(object? o)=>o is IEnumerable e?e.Cast<object>():Array.Empty<object>();
    static object? Selection(object? doc)=>Member(doc,"PrimarySelectedCollectionSelection");
    static object? ActiveLayer(object? variant)=>Member(Member(variant,"LocalAdjustmentsLayers"),"ActiveLayer");
    static string LayerId(object? variant)
    {
        var layer=ActiveLayer(variant);if(layer==null)return "Background";
        return IdOfLayer(layer);
    }
    static string IdOfLayer(object layer)
    {
        var id=Member(Member(layer,"Mask"),"GUID")?.ToString();
        if(!Guid.TryParse(id,out var guid)||guid==Guid.Empty)throw new InvalidOperationException("active layer has no stable native mask GUID");
        return guid.ToString();
    }
    static void RequireTarget(JsonElement request,object variant,object document,bool write)
    {
        var expected=request.GetProperty("expected_target");
        if(Member(variant,"UUID")?.ToString()!=expected.GetProperty("variant_uuid").GetString()||!string.Equals(Member(document,"Path")?.ToString(),expected.GetProperty("document_path").GetString(),StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("native target changed");
        if(!ReferenceEquals(Member(variant,"Document"),document)||((IntPtr?)Member(document,"BoundWindowHandle"))!=System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle)throw new InvalidOperationException("variant and document are not bound to the main window");
        var selection=Selection(document);var selected=Member(selection,"SelectedVariants");
        if(Convert.ToInt32(Member(selected,"Count"))!=1||Member(Member(selection,"PrimaryVariant"),"UUID")?.ToString()!=Member(variant,"UUID")?.ToString())throw new InvalidOperationException("exactly one matching primary variant required");
        if(expected.TryGetProperty("layer_uuid",out var layer)&&LayerId(variant)!=layer.GetString())throw new InvalidOperationException("native active layer changed");
        if(write)
        {
            if(!expected.TryGetProperty("document_uuid",out var documentUuid)||documentUuid.GetString()!=Member(document,"UUID")?.ToString())throw new InvalidOperationException("native document UUID changed");
            using var registered=JsonDocument.Parse(File.ReadAllText(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CaptureOneAI","candidates.json")));
            string uuid=Member(variant,"UUID")!.ToString()!;
            if(!registered.RootElement.TryGetProperty(uuid,out var record)||!string.Equals(record.GetProperty("document").GetProperty("path").GetString(),Member(document,"Path")?.ToString(),StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("writes require a registered test candidate");
            if(!string.Equals(record.GetProperty("image").GetProperty("path").GetString(),Member(Member(variant,"Image"),"FullpathFilename")?.ToString(),StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("candidate image changed");
            if(Member(variant,"IsLimitedReadOnly") is true)throw new InvalidOperationException("variant is read-only");
        }
    }
    static readonly Dictionary<string,(string Property,double Min,double Max)> Scalars=new()
    {
        ["exposure"]=("Exposure",-5,5),["contrast"]=("Contrast",-50,50),["brightness"]=("Brightness",-50,50),["saturation"]=("Saturation",-100,100),
        ["temperature"]=("WhiteBalanceTemperature",800,14000),["tint"]=("WhiteBalanceTint",-50,50),
        ["highlight_recovery"]=("HighlightRecovery",-100,100),["shadow_recovery"]=("ShadowRecovery",-100,100),["white_recovery"]=("WhiteRecovery",-100,100),["black_recovery"]=("BlackRecovery",-100,100),
        ["layer_opacity"]=("LayerOpacity",0,100),["clarity"]=("Clarity",-100,100),["structure"]=("ClarityStructure",-100,100),["dehaze"]=("DehazeAmount",-100,100)
    };
    static readonly Dictionary<string,string> Wheels=new(){["wheel_master"]="ColorBalance",["wheel_shadows"]="ColorBalanceShadow",["wheel_midtones"]="ColorBalanceMidtone",["wheel_highlights"]="ColorBalanceHighlight"};
    static readonly Dictionary<string,string> Curves=new(){["curve_rgb"]="GradationCurve",["curve_red"]="GradationCurveRed",["curve_green"]="GradationCurveGreen",["curve_blue"]="GradationCurveBlue",["curve_luma"]="GradationCurveY"};
    static readonly Dictionary<string,string> Mixers=new(){["basic_mixer"]="BasicColorCorrection",["advanced_mixer"]="AdvancedColorCorrections",["skin_mixer"]="SkinColorCorrections"};
    static readonly Dictionary<string,string> LevelChannels=new(){["rgb"]="RGB",["red"]="R",["green"]="G",["blue"]="B"};
    static readonly Dictionary<string,string> LevelProperties=new(){["input_black"]="Shadow",["input_white"]="Highlight",["midtone"]="Midtone",["output_black"]="TargetShadow",["output_white"]="TargetHighlight"};
    static Dictionary<string,double> ReadLevels(object variant,string channel)
        =>LevelProperties.ToDictionary(p=>p.Key,p=>Convert.ToDouble(ParameterValue(variant,p.Value+LevelChannels[channel]))*(p.Key=="midtone"?1:255));
    static object ReadMixer(object variant,string field)
    {
        var rows=Items(ParameterValue(variant,Mixers[field])).ToArray();
        string serialized=string.Concat(rows.Select(row=>Invoke(row,"Serialize")?.ToString()));
        if(field=="basic_mixer")return serialized;
        var ranges=rows.Select((row,index)=>new {index,type=Member(row,"Type")?.ToString(),rgb=new[]{Member(row,"DeviceColorRed"),Member(row,"DeviceColorGreen"),Member(row,"DeviceColorBlue")},
            hue=Member(row,"HueChange"),saturation=Member(row,"SatChange"),lightness=Convert.ToDouble(Member(row,"ValueChange"))*10,
            smoothness=Member(row,"Smoothness"),uniformity_hue=Member(row,"HomogeneityHue"),uniformity_saturation=Member(row,"HomogeneitySat"),uniformity_lightness=Member(row,"HomogeneityLightness"),
            hue_low=Member(row,"HueLow"),hue_high=Member(row,"HueHigh"),saturation_low=Member(row,"SatLow"),saturation_high=Member(row,"SatHigh"),enabled=Member(row,"IsEnabled"),inverted=Member(row,"IsInverted")}).ToArray();
        return new {serialized,ranges,selected_index=ParameterValue(variant,field=="skin_mixer"?"SelectedSkinColorCorrection":"SelectedAdvancedColorCorrection")};
    }
    static Type NativeType(string name)=>AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType(name,false)).First(t=>t!=null)!;
    static object Static(string type,string method,params object[] args)=>NativeType(type).GetMethods(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static).Single(m=>m.Name==method&&m.GetParameters().Length==args.Length).Invoke(null,args)!;
    const string WhiteBalanceHelper="P1.C1.ViewModels.Tools.WhiteBalance.WhiteBalanceHelper",WhiteBalanceConverter="P1.C1.ViewModels.Tools.WhiteBalance.WhiteBalanceConverter";
    static void RequireLayerProperty(object variant,object layer,string property)
    {
        if(Member(layer,"IsCloneOrHealLayer") is true)throw new InvalidOperationException("select an adjustment layer, not a repair layer");
        if(!property.StartsWith("Selected")&&Invoke(layer,"IsPropertyAvailable",property,Member(variant,"ProcessingEngine")) is not true)throw new InvalidOperationException("property unavailable on selected layer: "+property);
    }
    static object? ParameterValue(object variant,string property)
    {
        var layer=ActiveLayer(variant);if(layer==null)return Member(variant,property);
        bool white=property=="WhiteBalanceTemperature"||property=="WhiteBalanceTint";
        RequireLayerProperty(variant,layer,white?"WhiteBalance":property);
        if(white)
        {
            var rgb=Static(WhiteBalanceHelper,"GetWhiteBalance",layer,variant,true);
            var pair=Static(WhiteBalanceConverter,"WhiteBalanceToTemperatureTint",variant,rgb);
            return Member(pair,property=="WhiteBalanceTemperature"?"Item1":"Item2");
        }
        var getter=layer.GetType().GetMethods(Flags).Single(m=>m.Name=="GetValue"&&m.IsGenericMethodDefinition&&m.GetParameters().Length==1);
        return getter.MakeGenericMethod(Property(variant,property).PropertyType).Invoke(layer,new object[]{property});
    }
    static void SetParameter(object variant,string property,object value)
    {
        var layer=ActiveLayer(variant);if(layer==null){Property(variant,property).SetValue(variant,value);return;}
        bool white=property=="WhiteBalanceTemperature"||property=="WhiteBalanceTint";
        RequireLayerProperty(variant,layer,white?"WhiteBalance":property);
        if(white)
        {
            float temperature=property=="WhiteBalanceTemperature"?Convert.ToSingle(value):Convert.ToSingle(ParameterValue(variant,"WhiteBalanceTemperature"));
            float tint=property=="WhiteBalanceTint"?Convert.ToSingle(value):Convert.ToSingle(ParameterValue(variant,"WhiteBalanceTint"));
            value=Static(WhiteBalanceConverter,"TemperatureTintToWhiteBalance",variant,temperature,tint);property="WhiteBalance";
        }
        var setter=layer.GetType().GetMethods(Flags).Single(m=>m.Name=="SetValue"&&m.IsGenericMethodDefinition&&m.GetParameters().Length==2);
        setter.MakeGenericMethod(Property(variant,property).PropertyType).Invoke(layer,new[]{(object)property,value});
    }
    static object Orientation()=>Member(NativeType("P1.C1.ApplicationSettings").GetProperty("Default",BindingFlags.Public|BindingFlags.Static)!.GetValue(null),"ColorWheelOrientationInDegrees")!;
    const string BalanceHelper="P1.C1.Core.ColorUtility.ColorBalanceHelper",WheelMath="P1.C1.Tools.ColorBalance.ColorWheelMath";
    static object ReadWheel(object rgb)
    {
        var hs=Static(BalanceHelper,"GetHueAndSaturationScaled",rgb);
        var point=(System.Windows.Point)Static(WheelMath,"PositionFromHueAndSat",new Rect(-50,-50,100,100),hs,Orientation());
        return new {x=point.X,y=-point.Y,lightness=Convert.ToDouble(Static(BalanceHelper,"CalculateLightnes",rgb))*100-50};
    }
    static bool Equal(JsonElement a,JsonElement b)
    {
        if(a.ValueKind==JsonValueKind.Number&&b.ValueKind==JsonValueKind.Number)return a.GetDouble()==b.GetDouble();
        if(a.ValueKind!=b.ValueKind)return false;
        if(a.ValueKind==JsonValueKind.String)return a.GetString()==b.GetString();
        if(a.ValueKind==JsonValueKind.Object)return a.EnumerateObject().Count()==b.EnumerateObject().Count()&&a.EnumerateObject().All(p=>b.TryGetProperty(p.Name,out var other)&&Equal(p.Value,other));
        if(a.ValueKind==JsonValueKind.Array)return a.GetArrayLength()==b.GetArrayLength()&&a.EnumerateArray().Zip(b.EnumerateArray()).All(p=>Equal(p.First,p.Second));
        return a.GetRawText()==b.GetRawText();
    }
    static object ReadField(object variant,string field)
    {
        if(field=="mask_state")return MaskState(variant);
        if(Scalars.TryGetValue(field,out var entry))return field=="layer_opacity"?Member(ActiveLayer(variant)??throw new InvalidOperationException("select an adjustment layer"),entry.Property)!:ParameterValue(variant,entry.Property)!;
        if(Wheels.TryGetValue(field,out var wheel))return ReadWheel(ParameterValue(variant,wheel)!);
        if(Curves.TryGetValue(field,out var curve))return ((System.Drawing.PointF[])ParameterValue(variant,curve)!).Select(p=>new[]{p.X,p.Y}).ToArray();
        if(field.StartsWith("levels_")&&LevelChannels.ContainsKey(field.Substring(7)))return ReadLevels(variant,field.Substring(7));
        if(Mixers.ContainsKey(field))return ReadMixer(variant,field);
        throw new InvalidOperationException("unsupported native field: "+field);
    }
    static object Encode(object variant,string field,JsonElement value)
    {
        if(Scalars.TryGetValue(field,out var entry))
        {double number=value.GetDouble();if(!double.IsFinite(number)||number<entry.Min||number>entry.Max)throw new InvalidOperationException("native field out of range: "+field);return Convert.ChangeType(number,Property(field=="layer_opacity"?ActiveLayer(variant)??throw new InvalidOperationException("select an adjustment layer"):variant,entry.Property).PropertyType,CultureInfo.InvariantCulture);}
        if(Wheels.TryGetValue(field,out var wheel))
        {
            double x=value.GetProperty("x").GetDouble(),y=value.GetProperty("y").GetDouble(),l=value.GetProperty("lightness").GetDouble();
            if(!double.IsFinite(x)||!double.IsFinite(y)||!double.IsFinite(l)||x*x+y*y>2500.0001||l< -50||l>50)throw new InvalidOperationException("color wheel target outside native range");
            var rgb=ParameterValue(variant,wheel)!;var hs=Static(WheelMath,"HueAndSatFromPosition",new Rect(-50,-50,100,100),new System.Windows.Point(x,-y),Orientation());
            return Static(BalanceHelper,"ChangeLightness",Static(BalanceHelper,"GetRGBScaled",hs,rgb),(float)((l+50)/100));
        }
        if(Curves.ContainsKey(field))
        {
            var points=value.EnumerateArray().Select(p=>{var pair=p.EnumerateArray().Select(n=>n.GetSingle()).ToArray();if(pair.Length!=2||pair.Any(n=>!float.IsFinite(n)||n<0||n>1))throw new InvalidOperationException("invalid native curve point");return new System.Drawing.PointF(pair[0],pair[1]);}).ToArray();
            if(points.Length<2||points.Length>64||points.First().X!=0||points.Last().X!=1||points.Zip(points.Skip(1)).Any(p=>p.First.X>=p.Second.X))throw new InvalidOperationException("invalid native curve ordering");return points;
        }
        if(field.StartsWith("levels_")&&LevelChannels.ContainsKey(field.Substring(7)))
        {
            if(value.EnumerateObject().Count()!=5)throw new InvalidOperationException("all five channel levels required");
            var numbers=LevelProperties.Keys.ToDictionary(k=>k,k=>value.GetProperty(k).GetDouble());
            foreach(var pair in numbers)if(!double.IsFinite(pair.Value)||pair.Value<(pair.Key=="midtone"?-1:0)||pair.Value>(pair.Key=="midtone"?1:255))throw new InvalidOperationException("levels out of native range");
            if(numbers["input_black"]>=numbers["input_white"])throw new InvalidOperationException("input black must be below input white");
            var native=numbers.ToDictionary(p=>LevelProperties[p.Key]+LevelChannels[field.Substring(7)],p=>(float)(p.Value/(p.Key=="midtone"?1:255)));
            string suffix=LevelChannels[field.Substring(7)];
            if(native["Shadow"+suffix]>=native["Highlight"+suffix]||native["TargetShadow"+suffix]>native["TargetHighlight"+suffix])throw new InvalidOperationException("levels endpoints cross at native precision");
            return native;
        }
        if(field=="advanced_mixer"||field=="skin_mixer")return EncodeRange(variant,field,value);
        if(field=="basic_mixer")
        {
            string raw=value.GetString()??"";var list=ParameterValue(variant,"BasicColorCorrection")!;var old=Items(list).ToArray();
            var rows=raw.Split(';',StringSplitOptions.RemoveEmptyEntries);if(rows.Length!=old.Length||old.Length!=9)throw new InvalidOperationException("mixer must preserve all nine native rows");
            var type=old[0].GetType();var result=Array.CreateInstance(type,rows.Length);var decode=type.GetMethod("Deserialize",BindingFlags.Public|BindingFlags.Static)!;
            for(int i=0;i<rows.Length;i++)
            {
                var original=Invoke(old[i],"Serialize")!.ToString()!.TrimEnd(';').Split(',');var updated=rows[i].Split(',');
                if(updated.Length!=18||original.Length!=18)throw new InvalidOperationException("invalid mixer row");
                for(int k=0;k<18;k++){double number=double.Parse(updated[k],CultureInfo.InvariantCulture);if(!double.IsFinite(number)||((k<3||k>5)&&number!=double.Parse(original[k],CultureInfo.InvariantCulture)))throw new InvalidOperationException("mixer range identity must remain unchanged");}
                if(Math.Abs(double.Parse(updated[3],CultureInfo.InvariantCulture))>30||Math.Abs(double.Parse(updated[4],CultureInfo.InvariantCulture))>80||Math.Abs(double.Parse(updated[5],CultureInfo.InvariantCulture))>5)throw new InvalidOperationException("mixer HSL out of range");
                result.SetValue(decode.Invoke(null,new object[]{rows[i]+";"}),i);
            }
            return Activator.CreateInstance(list.GetType(),new object[]{result})!;
        }
        throw new InvalidOperationException("unsupported native field: "+field);
    }
    static object EncodeRange(object variant,string field,JsonElement input)
    {
        var list=ParameterValue(variant,Mixers[field]);var rows=Items(list).ToList();bool skin=field=="skin_mixer";
        string action=input.GetProperty("action").GetString()!;
        if(!new[]{"add","update","remove"}.Contains(action))throw new InvalidOperationException("unknown color range action");
        int index=action=="add"?rows.Count:input.GetProperty("index").GetInt32();
        if(index<0||action!="add"&&index>=rows.Count)throw new InvalidOperationException("color range index missing");
        object? color=null;
        if(input.TryGetProperty("rgb",out var rgb))
        {
            var components=rgb.EnumerateArray().Select(c=>c.GetSingle()).ToArray();
            if(components.Length!=3||components.Any(c=>!float.IsFinite(c)||c<0||c>255))throw new InvalidOperationException("native device RGB requires 0..255");
            color=Static("P1.C1.Common.OpenCore.MultiColor","FromRGB",components[0],components[1],components[2],false);
        }
        if(action=="add")
        {
            if(color==null)throw new InvalidOperationException("new color range requires RGB");
            if(skin&&rows.Count!=0)throw new InvalidOperationException("Skin Tone has one range; update its existing index instead");
            rows.Add(Static("P1.C1.Core.ColorCorrectionInteropUtils",skin?"SkinColorCorrectionFromPick":"AdvancedColorCorrectionFromPick",color));
        }
        if(action=="remove")rows.RemoveAt(index);
        else
        {
            var settings=new Dictionary<string,(int Index,float Min,float Max,float Scale)>{
                ["hue"]=(2,skin?-5:-30,skin?5:30,1),["lightness"]=(3,skin?-10:-100,skin?10:100,10),
                ["saturation"]=(4,skin?-30:-100,skin?30:200,1),["smoothness"]=(5,1,30,1),
                ["hue_low"]=(12,-357,-2,1),["hue_high"]=(13,2,357,1),["saturation_low"]=(11,-float.MaxValue,-2,1),["saturation_high"]=(10,2,float.MaxValue,1)};
            if(skin){settings["uniformity_hue"]=(6,0,100,1);settings["uniformity_saturation"]=(7,0,100,1);settings["uniformity_lightness"]=(8,0,100,1);}
            object?[] args=new object?[15];args[0]=rows[index];args[1]=action=="add"?null:color;
            foreach(var item in input.EnumerateObject())
            {
                if(settings.TryGetValue(item.Name,out var setting))
                {float number=item.Value.GetSingle();if(!float.IsFinite(number)||number<setting.Min||number>setting.Max)throw new InvalidOperationException("native range value outside UI limits: "+item.Name);args[setting.Index]=number/setting.Scale;}
                else if(item.Name=="inverted")args[9]=item.Value.GetBoolean();
                else if(item.Name=="enabled")args[14]=item.Value.GetBoolean();
                else if(!new[]{"action","index","rgb"}.Contains(item.Name))throw new InvalidOperationException("unsupported range field: "+item.Name);
            }
            float low=args[12] is float lo?lo:Convert.ToSingle(Member(rows[index],"HueLow"));
            float high=args[13] is float hi?hi:Convert.ToSingle(Member(rows[index],"HueHigh"));
            if(high-low>359)throw new InvalidOperationException("native hue selection cannot span more than 359 degrees");
            rows[index]=Static("P1.C1.Core.ColorCorrectionInteropUtils","CreateCCUsingMultiColorAndOtherCC",args!);
        }
        var array=Array.CreateInstance(NativeType("P1.ModelCoreWrapper.ColorCorrection"),rows.Count);
        for(int i=0;i<rows.Count;i++)array.SetValue(rows[i],i);
        return new RangeWrite{List=Activator.CreateInstance(list?.GetType()??NativeType("P1.ModelCoreWrapper.ColorCorrectionList"),new object[]{array})!,Added=action=="add"?rows[index]:null,RemovedIndex=action=="remove"?index:-1,Count=rows.Count};
    }
    static void SetField(object variant,string field,object value)
    {
        if(value is RangeWrite range)
        {
            string selected=field=="skin_mixer"?"SelectedSkinColorCorrection":"SelectedAdvancedColorCorrection";
            if(range.Added!=null)
            {
                if(!(bool)Static("P1.C1.Tools.SelectiveColor.ColorCorrectionListHelper","AddCorrection",range.Added,variant,Mixers[field],selected,ActiveLayer(variant)==null))throw new InvalidOperationException("C1 declined adding the color range");
            }
            else
            {
                int previous=Convert.ToInt32(ParameterValue(variant,selected));
                SetParameter(variant,Mixers[field],range.List);
                if(range.RemovedIndex>=0)
                {
                    int current=previous>range.RemovedIndex?previous-1:previous;
                    current=Math.Clamp(current,-1,range.Count-1);
                    SetParameter(variant,selected,current);
                }
            }
            return;
        }
        if(field.StartsWith("levels_"))
        {
            var levels=(Dictionary<string,float>)value;string suffix=LevelChannels[field.Substring(7)];
            foreach(string prefix in new[]{"","Target"})
            {
                string black=prefix+"Shadow"+suffix,white=prefix+"Highlight"+suffix;
                bool whiteFirst=levels[black]>=Convert.ToSingle(ParameterValue(variant,white));
                foreach(string levelProperty in whiteFirst?new[]{white,black}:new[]{black,white})SetParameter(variant,levelProperty,levels[levelProperty]);
            }
            SetParameter(variant,"Midtone"+suffix,levels["Midtone"+suffix]);return;
        }
        string property=Mixers.GetValueOrDefault(field)??Wheels.GetValueOrDefault(field)??Curves.GetValueOrDefault(field)??Scalars[field].Property;
        if(field=="layer_opacity")Property(ActiveLayer(variant)!,property).SetValue(ActiveLayer(variant),value);
        else SetParameter(variant,property,value);
    }
    static object Describe(object? o)
    {
        if(o==null)return new {type="null"};
        var t=o.GetType();
        var properties=t.GetProperties(Flags).Where(p=>p.GetIndexParameters().Length==0).Select(p=>new {name=p.Name,type=p.PropertyType.FullName,write=p.CanWrite}).ToArray();
        var fields=new List<object>();
        for(Type? p=t;p!=null;p=p.BaseType)fields.AddRange(p.GetFields(Flags|BindingFlags.DeclaredOnly).Select(f=>(object)new {name=f.Name,type=f.FieldType.FullName}));
        return new {type=t.FullName,properties,fields};
    }
    static object? Variant(List<object> models)
    {
        var basic=models.FirstOrDefault(o=>Name(o).Contains("BasicColorEditorViewModel"));
        var primary=Member(Member(basic,"m_colorCorrectionsPresenter"),"PrimaryVariant");if(primary!=null)return primary;
        var white=models.FirstOrDefault(o=>Name(o)=="P1.C1.ViewModels.Tools.WhiteBalance.WhiteBalanceToolViewModel");
        var variant=Member(white,"m_variant");if(variant!=null)return variant;
        var queue=new Queue<(object,int)>();var seen=new HashSet<object>(ReferenceEqualityComparer.Instance);
        foreach(var m in models.Where(o=>Name(o).Contains("ExposureToolViewModel")||Name(o).Contains("BasicColorEditorViewModel")))queue.Enqueue((m,0));
        string[] paths={"m_colorCorrectionsPresenter","m_primaryVariantPresenter","m_primaryVariantPropertyChangedPresenter","PrimaryVariant","Document","SelectedCollection","SelectedVariants","Current","m_variant","ExposurePresenter","m_presenter","m_primaryVariantPropertyPresenter"};
        while(queue.Count>0)
        {
            var (x,d)=queue.Dequeue();if(!seen.Add(x))continue;
            if(x.GetType().GetInterfaces().Any(i=>i.FullName=="P1.C1.Core.ICoreVariant"))return x;
            if(d>=5)continue;
            foreach(string key in paths){object? v=null;try{v=Member(x,key);}catch{}if(v!=null&&!v.GetType().IsValueType&&v is not string)queue.Enqueue((v,d+1));}
        }
        return null;
    }
    static object? Document(List<object> models)
    {
        var basic=models.FirstOrDefault(o=>Name(o).Contains("BasicColorEditorViewModel"));
        var document=Member(Member(basic,"m_colorCorrectionsPresenter"),"Document")??Member(Variant(models),"Document");
        if(document==null)
        {
            var roots=models.Concat(System.Windows.Forms.Application.OpenForms.Cast<Form>().Where(f=>f.Handle==System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle).Cast<object>());
            foreach(var root in roots)
            foreach(string key in new[]{"Document","m_document"})
            {
                object? candidate=null;try{candidate=Member(root,key);}catch{}
                if(candidate!=null&&candidate.GetType().GetInterfaces().Any(t=>t.FullName=="P1.C1.Core.ModelEntities.ICaptureOneDocument")&&((IntPtr?)Member(candidate,"BoundWindowHandle"))==System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle){document=candidate;break;}
            }
        }
        if(document!=null)cachedDocument=document;
        return document??cachedDocument;
    }
    static object Target(object? variant,object? document)=>new {variant_uuid=Member(variant,"UUID")?.ToString(),variant_type=variant?.GetType().FullName,document_path=Member(document,"Path")?.ToString(),document_uuid=Member(document,"UUID")?.ToString()};
    static void SelectOnly(object document,object chosen)
    {
        var selected=Member(Selection(document),"SelectedVariants")!;
        var method=selected.GetType().GetMethods(Flags).Single(m=>m.Name=="SelectOnly"&&m.GetParameters().Length==1);
        var array=Array.CreateInstance(method.GetParameters()[0].ParameterType.GetElementType()!,1);array.SetValue(chosen,0);
        method.Invoke(selected,new object[]{array});
        if(Convert.ToInt32(Member(selected,"Count"))!=1||Member(Member(Selection(document),"PrimaryVariant"),"UUID")?.ToString()!=Member(chosen,"UUID")?.ToString())throw new InvalidOperationException("native selection readback differs");
    }
    static object CurrentCollection(object document)=>Member(Selection(document),"SelectedCollection")??throw new InvalidOperationException("no active collection");
    static object[] CollectionVariants(object document,bool filtered)
    {
        var collection=CurrentCollection(document);
        if(Member(collection,"IsLoading") is true||Member(collection,"Loaded") is not true)throw new InvalidOperationException("C1 collection is still loading");
        return Items(Invoke(collection,filtered?"GetAllFilteredVariants":"GetAllVariants")).ToArray();
    }
    static object ImageInventory(object document,JsonElement request)
    {
        bool filtered=!request.TryGetProperty("scope",out var scope)||scope.GetString()!="all";
        var collection=CurrentCollection(document);var variants=CollectionVariants(document,filtered);
        int offset=request.TryGetProperty("offset",out var o)?o.GetInt32():0,limit=request.TryGetProperty("limit",out var l)?l.GetInt32():200;
        if(offset<0||limit<1||limit>500)throw new InvalidOperationException("inventory offset/limit outside bounds");
        string collectionId=Member(collection,"UUID")!.ToString()!;
        string token=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(collectionId+":"+filtered+":"+string.Join(",",variants.Select(v=>Member(v,"UUID")!.ToString())))));
        if(request.TryGetProperty("collection_token",out var expected)&&expected.GetString()!=token)throw new InvalidOperationException("collection changed during pagination; list again");
        var selected=Items(Member(Selection(document),"SelectedVariants")).Select(v=>Member(v,"UUID")!.ToString()).ToHashSet();
        var rows=variants.Skip(offset).Take(limit).Select((v,i)=>new {index=offset+i,variant_uuid=Member(v,"UUID")?.ToString(),variant_id=Member(v,"DatabaseId"),filename=Member(Member(v,"Image"),"FileName"),path=Member(Member(v,"Image"),"FullpathFilename"),selected=selected.Contains(Member(v,"UUID")!.ToString())}).ToArray();
        return new {items=rows,collection_uuid=collectionId,collection_name=Member(collection,"Name"),collection_token=token,scope=filtered?"filtered_collection":"all_collection",total=variants.Length,offset,next_offset=offset+rows.Length<variants.Length?(int?)(offset+rows.Length):null,enumeration_complete=offset==0&&rows.Length==variants.Length,virtualized=false,source="c1_internal_model",
            navigation=new {document_path=Member(document,"Path"),document_uuid=Member(document,"UUID")?.ToString(),selected_uuids=selected.OrderBy(s=>s).ToArray()}};
    }
    static object Api(object? o)
    {
        if(o==null)return new {type="null"};
        return new {description=Describe(o),constructors=o.GetType().GetConstructors().Select(c=>c.ToString()).ToArray(),methods=o.GetType().GetMethods(Flags|BindingFlags.Static).Where(m=>!m.IsSpecialName).Select(m=>m.ToString()).Distinct().ToArray()};
    }
    static object[] Layers(object variant)
    {
        int pid=Environment.ProcessId;int variantId=Convert.ToInt32(Member(variant,"DatabaseId"));
        var rows=new List<object>{new {runtime_id=new[]{pid,variantId,0},name="Background",is_image_layer=true,native_id="Background",selected=ActiveLayer(variant)==null}};
        foreach(var layer in Items(Member(variant,"LocalAdjustmentsLayers")))
        {
            string id=IdOfLayer(layer);byte[] bytes=Guid.Parse(id).ToByteArray();var reference=new[]{pid,variantId,BitConverter.ToInt32(bytes,0),BitConverter.ToInt32(bytes,4),BitConverter.ToInt32(bytes,8),BitConverter.ToInt32(bytes,12)};
            rows.Add(new {runtime_id=reference,name=Member(layer,"Name")?.ToString(),is_image_layer=false,native_id=id,object_id=System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(layer),selected=ReferenceEquals(layer,ActiveLayer(variant)),opacity=Member(layer,"LayerOpacity"),enabled=Member(layer,"LayerEnabled"),mask=id,generated=Member(layer,"GeneratedMaskContents")?.ToString()});
        }
        return rows.ToArray();
    }
    static object EnumValue(string type,string value)=>Enum.Parse(NativeType(type),value);
    static object Fields(object value)=>value.GetType().GetFields(BindingFlags.Public|BindingFlags.Instance).ToDictionary(f=>f.Name,f=>f.GetValue(value));
    static object? MaskParameters(string method,object handle)
    {
        var api=NativeType("ImgCore.ImgCoreDll").GetMethods(BindingFlags.Public|BindingFlags.Static).Single(m=>m.Name==method);
        object?[] args={handle,null};var result=api.Invoke(null,args);
        return Convert.ToInt32(result)==0?args[1]:null;
    }
    static object MaskState(object variant)
    {
        var layer=ActiveLayer(variant);if(layer==null)return new {applicable=false};
        var mask=Member(layer,"Mask")!;var handle=Invoke(mask,"GetHandle")!;
        var selectedHandle=Invoke(mask,"GetHandleToWorkWith")!;
        var fill=MaskParameters("IC_MaskConstantFillGet",selectedHandle);var composition=MaskParameters("IC_MaskCompositionGet",selectedHandle);
        string revision=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(Invoke(handle,"DangerousGetHandle")!.ToString()!))).Substring(0,16);
        var linear=Invoke(mask,"GetMaskLinearGradient");var radial=Invoke(mask,"GetRadialMask");
        var luma=Invoke(mask,"TryGetLumaRange",(object?)null);
        // C1's UI list is reversed; expose the actual native index used by setters.
        var subs=Items(Member(layer,"SubmasksInformation")).Reverse().Select((s,index)=>new {index,name=Member(s,"Name"),blend=Member(s,"BlendMode")?.ToString(),luma=Member(s,"IsLumaMask"),linear=Member(s,"IsLinearGradient"),radial=Member(s,"IsRadialGradient")}).ToArray();
        return new {applicable=true,constant_fill=fill==null?null:Fields(fill),composition=composition==null?null:Fields(composition),parametric=Member(layer,"IsParametric"),buffered=Invoke(mask,"HasBufferedDrawingOperations"),content_revision=revision,
            selected_submask=Member(layer,"SelectedSubmaskIndex"),submasks=subs,luma=luma==null?null:Fields(luma),
            linear=linear==null?null:new {start=Fields(Member(linear,"start")!),stop=Fields(Member(linear,"stop")!),midpoint=Member(linear,"midpoint")},
            radial=radial==null?null:new {center=Member(radial,"CenterPointRaw"),radius_x=Member(radial,"RadiusHorizontalRaw"),radius_y=Member(radial,"RadiusVerticalRaw"),falloff=Member(radial,"FalloffStrength"),angle=Member(Member(radial,"RotationAngle"),"Value"),fill=Member(radial,"FillOptions")?.ToString()},
            raw_dimensions=Member(variant,"ImageDimensions")};
    }
    static void ChangeMask(object layer,object variant,string label,Action<object> change)
    {
        // Let C1 close its undo group even if the supplied native operation fails.
        Exception? failure=null;
        Action<object> guarded=m=>{try{change(m);}catch(Exception e){failure=e;}};
        Invoke(layer,"PerformUndoableMaskContentChange",guarded,variant,label,false,null);
        if(failure!=null)throw failure;
    }
    static void AddMask(object layer,object handle,string type,string blend,object variant,object? generated=null,object? skin=null)
        =>Invoke(layer,"AddSubmask",handle,EnumValue("P1.C1.Core.LocalAdjustments.Wrapper.Interfaces.Submask",type),EnumValue("ImgCore.IC_CompositeMaskBlendMode",blend),
            generated??Enum.ToObject(NativeType("P1.C1.Core.LocalAdjustments.LocalAdjustmentsMask+GeneratedMaskContentsType"),0),
            skin??Activator.CreateInstance(NativeType("P1.C1.Core.LocalAdjustments.LocalAdjustmentsMask+SkinMaskType")),
            Items(Member(variant,"LocalAdjustmentsLayers")).Select(l=>Member(l,"Name")!.ToString()!).ToArray());
    static double Number(JsonElement p,string key,double low,double high)
    {double x=p.GetProperty(key).GetDouble();if(!double.IsFinite(x)||x<low||x>high)throw new InvalidOperationException("out of range: "+key);return x;}
    static string[] ModuleFields(string module)=>module switch {
        "exposure"=>new[]{"exposure","contrast","brightness","saturation"},"white_balance"=>new[]{"temperature","tint"},
        "hdr"=>new[]{"highlight_recovery","shadow_recovery","white_recovery","black_recovery"},
        "curves"=>Curves.Keys.ToArray(),"levels"=>LevelChannels.Keys.Select(c=>"levels_"+c).ToArray(),
        "color_mixer"=>Mixers.Keys.ToArray(),"color_balance"=>Wheels.Keys.ToArray(),_=>throw new InvalidOperationException("unsupported module operation")};
    static string[] ModuleKeys(string module)=>ModuleFields(module).SelectMany(f=>
        f=="temperature"||f=="tint"?new[]{"WhiteBalance"}:
        f.StartsWith("levels_")?LevelProperties.Values.Select(p=>p+LevelChannels[f.Substring(7)]):
        new[]{Mixers.GetValueOrDefault(f)??Wheels.GetValueOrDefault(f)??Curves.GetValueOrDefault(f)??Scalars[f].Property}).Distinct().ToArray();
    static object DecodeMixerSnapshot(object variant,string field,JsonElement value)
    {
        string raw=value.GetProperty("serialized").GetString()!;
        if(raw.Length>131072)throw new InvalidOperationException("mixer snapshot too large");
        var listType=NativeType("P1.ModelCoreWrapper.ColorCorrectionList");
        var type=NativeType("P1.ModelCoreWrapper.ColorCorrection");
        var rows=raw.Split(';',StringSplitOptions.RemoveEmptyEntries);
        if(rows.Length>(field=="skin_mixer"?1:64))throw new InvalidOperationException("too many mixer ranges");
        var array=Array.CreateInstance(type,rows.Length);
        for(int i=0;i<rows.Length;i++)array.SetValue(type.GetMethod("Deserialize",BindingFlags.Public|BindingFlags.Static)!.Invoke(null,new object[]{rows[i]+";"}),i);
        return Activator.CreateInstance(listType,new object[]{array})!;
    }
    static object ViewerGeometry()
    {
        var found=new List<FrameworkElement>();
        foreach(Form form in System.Windows.Forms.Application.OpenForms)
        if(form.Handle==System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle)
        foreach(var control in Controls(form))
        if(control is ElementHost host&&host.Child!=null)
        foreach(var node in Visuals(host.Child))
        if(node is FrameworkElement fe&&fe.GetType().Name=="VariantGridItemWpf"&&fe.Name=="Viewer"&&fe.IsVisible&&fe.ActualWidth>32&&fe.ActualHeight>32)found.Add(fe);
        if(found.Count!=1)throw new InvalidOperationException("exactly one native Viewer required");
        var view=found[0];var a=view.PointToScreen(new System.Windows.Point(0,0));var b=view.PointToScreen(new System.Windows.Point(view.ActualWidth,view.ActualHeight));
        return new {automation_id="Viewer",class_name="VariantGridItemWpf",control_type="Custom",offscreen=false,enabled=view.IsEnabled,runtime_id=new[]{Environment.ProcessId,System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(view)},rect=new {x=a.X,y=a.Y,width=b.X-a.X,height=b.Y-a.Y},native_type=Name(view)};
    }
    static string ProcessRevision(object variant)
    {
        var counters=Member(Member(variant,"Settings"),"VariantProcessCounters")??throw new InvalidOperationException("native process counters unavailable");
        return string.Join(":",new[]{"HighResCounter","LowResCounter","SyncCounter"}.Select(k=>Member(counters,k)??throw new InvalidOperationException("native process counter missing")));
    }
    static void StructField(object value,string name,object? fieldValue)=>value.GetType().GetField(name)!.SetValue(value,fieldValue);
    static object Preview(object document,object variant,JsonElement request)
    {
        RequireTarget(request,variant,document,false);
        string action=request.GetProperty("scope").GetString()!,kind=request.TryGetProperty("kind",out var k)?k.GetString()!:"native";
        if(!new[]{"preview_start","preview_result","preview_map"}.Contains(action))throw new InvalidOperationException("unsupported preview action");
        int size=request.TryGetProperty("max_size",out var s)?s.GetInt32():1600;
        if(kind!="native"&&kind!="mask"||size<128||size>1600)throw new InvalidOperationException("invalid native preview kind/size");
        string revision=ProcessRevision(variant);
        bool pixelBuffer=request.TryGetProperty("pixel_buffer",out var pb)&&pb.GetBoolean();
        PreviewJob? currentJob=pixelBuffer?pixelPreviewJob:previewJob;
        if(pixelBuffer&&kind!="native")throw new InvalidOperationException("pixel buffer requires native photo render");
        string key=Member(document,"UUID")+":"+Member(variant,"UUID")+":"+revision+":"+kind+":"+size+":"+LayerId(variant)+":"+pixelBuffer;
        if(action=="preview_start")
        {
            if(currentJob!=null&&!currentJob.Work.IsCompleted)
            {
                if(currentJob.Key!=key)throw new InvalidOperationException("native preview still rendering; poll its id before starting another");
            }
            else if(currentJob==null||currentJob.Key!=key||currentJob.Work.IsFaulted||currentJob.Work.IsCanceled||!File.Exists(Member(currentJob.Work.Result,"path")?.ToString()))
            {
                // Snapshot all model data on C1's UI thread, then let C1's own scheduler render off-thread.
                object settings=kind=="mask"?Invoke(Invoke(variant,"GetProcessSettingsNoLocalAdjLayers",true)!,"Get")!:Invoke(variant,"GetICProcessSettings")!;
                var geometry=Static("P1.C1.Core.VariantGeometryTransformer","FromProcessSettings",settings)!;
                var crop=Member(geometry,"CropRawConstrained")!;
                float width=Convert.ToSingle(Member(crop,"Width")),height=Convert.ToSingle(Member(crop,"Height"));
                if(width<=0||height<=0)throw new InvalidOperationException("invalid native crop dimensions");
                foreach(string field in new[]{"CenterX","CenterY","Width","Height"})StructField(settings,"crop"+field,Member(crop,field));
                StructField(settings,"cropScaleView",Math.Min(1f,size/Math.Max(width,height)));
                if(kind=="mask")
                {
                    var layer=ActiveLayer(variant)??throw new InvalidOperationException("mask preview requires an adjustment layer");
                    var handle=Invoke(Member(layer,"Mask")!,"GetHandle")!;
                    StructField(settings,"localAdjustShowHdl",handle);
                    StructField(settings,"localAdjustMaskDisplayType",EnumValue("ImgCore.IC_MaskDisplayType","mdtGreyscale"));
                    StructField(settings,"localAdjustShowAlpha",255);
                    StructField(settings,"localAdjustShowColor",Activator.CreateInstance(NativeType("ImgCore.IC_RGB32"),new object[]{255,255,255}));
                    Invoke(settings,"SetLocalAdjustmentsMaskHandle",0,handle);
                }
                var bitmap=Activator.CreateInstance(NativeType("P1.C1.ImageCoreWrapper.ImageCore.ImageCoreBitmap"))!;
                try
                {
                    var method=NativeType("P1.C1.Components.VariantProcessor").GetMethods(BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic).Single(m=>m.Name=="ProcessToBitmapScheduledAsync");
                    object?[] args={settings,"C1 AI Preview",EnumValue("P1.C1.ImageCoreWrapper.ImageCore.ProcessQuality","PreviewHigh"),variant,document,bitmap,null,null,Enum.ToObject(method.GetParameters()[8].ParameterType,1),false};
                    var task=(Task)method.Invoke(null,args)!;
                    var job=new PreviewJob{Key=key,Revision=revision,Kind=kind,Geometry=geometry};
                    job.Work=Task.Run(()=>FinishPreview(task,bitmap,settings,pixelBuffer));
                    currentJob=job;if(pixelBuffer)pixelPreviewJob=job;else previewJob=job;
                }
                catch{((IDisposable)bitmap).Dispose();throw;}
            }
            return new {preview_id=currentJob.Id,phase=currentJob.Work.IsCompleted?"ready":"rendering",revision};
        }
        var current=currentJob??throw new InvalidOperationException("preview expired; render again");
        if(request.GetProperty("preview_id").GetString()!=current.Id||current.Key!=key)throw new InvalidOperationException("preview target or adjustments changed; render again");
        if(!current.Work.IsCompleted)return new {preview_id=current.Id,phase="rendering",revision};
        object artifact=current.Work.GetAwaiter().GetResult();
        var encoded=JsonSerializer.SerializeToElement(artifact);
        int w=encoded.GetProperty("width").GetInt32(),h=encoded.GetProperty("height").GetInt32();
        var canvas=Member(current.Geometry,"CropCanvasConstrained")!;
        double cw=Convert.ToDouble(Member(canvas,"Width")),ch=Convert.ToDouble(Member(canvas,"Height")),left=Convert.ToDouble(Member(canvas,"CenterX"))-cw/2,top=Convert.ToDouble(Member(canvas,"CenterY"))-ch/2;
        object Map(JsonElement points,string direction)
        {
            if(points.ValueKind!=JsonValueKind.Array||points.GetArrayLength()>256)throw new InvalidOperationException("at most 256 points required");
            var input=points.EnumerateArray().Select(p=>new System.Drawing.PointF((float)Number(p,"x",-1000000,1000000),(float)Number(p,"y",-1000000,1000000))).ToArray();
            System.Drawing.PointF[] output;
            if(direction=="preview_to_raw")
            {
                if(input.Any(p=>p.X<0||p.X>w||p.Y<0||p.Y>h))throw new InvalidOperationException("preview point outside bitmap");
                output=(System.Drawing.PointF[])Invoke(current.Geometry,"Canvas2Raw",(object)input.Select(p=>new System.Drawing.PointF((float)(left+p.X*cw/w),(float)(top+p.Y*ch/h))).ToArray())!;
            }
            else if(direction=="raw_to_preview")
                output=((System.Drawing.PointF[])Invoke(current.Geometry,"Raw2Canvas",(object)input)!).Select(p=>new System.Drawing.PointF((float)((p.X-left)*w/cw),(float)((p.Y-top)*h/ch))).ToArray();
            else throw new InvalidOperationException("invalid coordinate direction");
            return output.Select(p=>new {x=p.X,y=p.Y}).ToArray();
        }
        if(action=="preview_map")return new {preview_id=current.Id,phase="ready",revision,points=Map(request.GetProperty("points"),request.GetProperty("direction").GetString()!)};
        var corners=JsonSerializer.SerializeToElement(new[]{new{x=0,y=0},new{x=w,y=0},new{x=w,y=h},new{x=0,y=h},new{x=w/2,y=h/2}});
        return new {preview_id=current.Id,phase="ready",revision,artifact,kind=current.Kind,
            geometry=new {width=w,height=h,raw_dimensions=Member(variant,"ImageDimensions"),coordinate_convention="pixel edges; top-left origin; preview_map uses C1 nonlinear transform",raw_corners_and_center=Map(corners,"preview_to_raw"),source="VariantGeometryTransformer"}};
    }
    static async Task<object> FinishPreview(Task render,object bitmap,object settings,bool pixelBuffer)
    {
        string? temporary=null;
        try
        {
            var timer=System.Diagnostics.Stopwatch.StartNew();
            await render.ConfigureAwait(false);
            double renderWait=timer.Elapsed.TotalSeconds;
            if(Convert.ToInt32(Member(render,"Result"))!=0)throw new InvalidOperationException("native renderer returned "+Member(render,"Result"));
            int width=Convert.ToInt32(Member(bitmap,"Width")),height=Convert.ToInt32(Member(bitmap,"Height"));
            if(width<1||height<1||width>1700||height>1700)throw new InvalidOperationException("native bitmap dimensions outside preview bounds");
            if(pixelBuffer)
            {
                // Owned native bitmap copy, not screen capture or arbitrary process-memory access.
                var source=(System.Windows.Media.Imaging.BitmapSource)Invoke(bitmap,"ToBitmapSourceDeepCopy",false)!;
                if(source.PixelWidth!=width||source.PixelHeight!=height)throw new InvalidOperationException("native pixel dimensions changed");
                if(source.Format!=PixelFormats.Bgra32&&source.Format!=PixelFormats.Bgr32&&source.Format!=PixelFormats.Pbgra32&&source.Format!=PixelFormats.Rgb24)
                    throw new InvalidOperationException("unsupported native pixel format: "+source.Format);
                var converted=source.Format==PixelFormats.Bgra32?source:new System.Windows.Media.Imaging.FormatConvertedBitmap(source,PixelFormats.Bgra32,null,0);
                int stride=checked(width*4);byte[] pixels=new byte[checked(stride*height)];converted.CopyPixels(pixels,stride,0);
                double copyDone=timer.Elapsed.TotalSeconds;
                string rawRoot=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CaptureOneAI","native-pixels");Directory.CreateDirectory(rawRoot);
                string rawHash=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(pixels)).ToLowerInvariant();
                string rawPath=Path.Combine(rawRoot,rawHash+".bgra");
                temporary=Path.Combine(rawRoot,Guid.NewGuid().ToString("N")+".tmp");File.WriteAllBytes(temporary,pixels);File.Move(temporary,rawPath,true);temporary=null;
                return new {path=rawPath,sha256=rawHash,width,height,stride,format="BGRA8",row_order="top-down",bit_depth=8,bytes=pixels.Length,
                    timing_seconds=new {render_wait=renderWait,pixel_copy=copyDone-renderWait,hash_and_write=timer.Elapsed.TotalSeconds-copyDone}};
            }
            string root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CaptureOneAI","native-previews");Directory.CreateDirectory(root);
            temporary=Path.Combine(root,Guid.NewGuid().ToString("N")+".tmp.png");
            if(Convert.ToInt32(Invoke(bitmap,"DumpToFileAsPng",temporary))!=0)throw new InvalidOperationException("native PNG encoding failed");
            string hash=Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(temporary))).ToLowerInvariant();
            string path=Path.Combine(root,hash+".png");File.Move(temporary,path,true);temporary=null;
            return new {path,sha256=hash,width,height,format="PNG",bytes=new FileInfo(path).Length,
                timing_seconds=new {render_wait=renderWait,png_encode_hash_write=timer.Elapsed.TotalSeconds-renderWait}};
        }
        finally{((IDisposable)bitmap).Dispose();GC.KeepAlive(settings);if(temporary!=null)File.Delete(temporary);}
    }
    public static string Handle(string json)
    {
        bool dispatched=false;
        try
        {
            using var input=JsonDocument.Parse(json);var request=input.RootElement;string command=request.GetProperty("command").GetString()??"";
            if(!new[]{"inspect","context","viewer","read","write","select","clone","layer","module"}.Contains(command))throw new InvalidOperationException("unsupported internal command");
            var models=Models();var document=Document(models);var variant=Member(Selection(document),"PrimaryVariant")??Variant(models);
            var basic=models.FirstOrDefault(o=>Name(o).Contains("BasicColorEditorViewModel"));
            object data;
            if(command=="inspect"&&request.TryGetProperty("types",out var apiTypes))
            {
                data=apiTypes.EnumerateArray().ToDictionary(n=>n.GetString()!,n=>{var t=NativeType(n.GetString()!);return (object)new {
                    constructors=t.GetConstructors(Flags).Select(c=>c.ToString()).ToArray(),
                    methods=t.GetMethods(Flags|BindingFlags.Static).Where(m=>!m.IsSpecialName).Select(m=>m.ToString()).Distinct().ToArray(),
                    properties=t.GetProperties(Flags).Select(p=>new {name=p.Name,type=p.PropertyType.FullName,write=p.CanWrite}).ToArray(),
                    fields=t.GetFields(Flags|BindingFlags.Static).Select(f=>new {name=f.Name,type=f.FieldType.FullName,value=f.IsLiteral?f.GetRawConstantValue():null}).ToArray()};});
            }
            else if(command=="inspect"&&request.TryGetProperty("kind",out var navKind)&&navKind.GetString()=="navigation")
            {var collection=Member(Selection(document),"SelectedCollection");data=new {collection=Api(collection),selection=Describe(Selection(document)),document=Api(document),variant=Api(variant),image_handle=Api(Member(Member(variant,"CoreImage"),"Handle"))};}
            else if(command=="inspect"&&request.TryGetProperty("kind",out var localKind)&&localKind.GetString()=="local_api")
            {
                var layer=ActiveLayer(variant)??throw new InvalidOperationException("select an adjustment layer");
                var properties=Scalars.Values.Select(v=>v.Property).Concat(Wheels.Values).Concat(Curves.Values).Concat(Mixers.Values).Concat(new[]{"WhiteBalance","ShadowRGB","HighlightRGB"}).Distinct();
                data=properties.ToDictionary(p=>p,p=>{object? value=Invoke(layer,"GetValueUntyped",p);return (object)new {available=Invoke(layer,"IsPropertyAvailable",p,Member(variant,"ProcessingEngine")),value_type=value?.GetType().FullName,text=value?.ToString(),contains=Invoke(layer,"Contains",p)};});
            }
            else if(command=="inspect"&&request.TryGetProperty("kind",out var kind)&&kind.GetString()=="color_api")
            {
                var type=NativeType("P1.C1.Common.OpenCore.MultiColor");
                data=new {constructors=type.GetConstructors().Select(c=>c.ToString()).ToArray(),methods=type.GetMethods(BindingFlags.Public|BindingFlags.Static).Select(m=>m.ToString()).ToArray(),helpers=NativeType("P1.C1.Core.ColorCorrectionInteropUtils").GetMethods(BindingFlags.Public|BindingFlags.Static).Select(m=>m.ToString()).ToArray()};
            }
            else if(command=="inspect") data=new {target=Target(variant,document),model_types=models.Select(Name).Distinct().ToArray(),variant=Describe(variant),document=Describe(document),basic=Describe(basic),presenter=Describe(Member(basic,"m_colorCorrectionsPresenter")),selection=Api(Member(document,"PrimarySelectedCollectionSelection")),selected_variants=Api(Member(Selection(document),"SelectedVariants")),collection=Api(Member(document,"ServedCollection")),layers=Api(Member(variant,"LocalAdjustmentsLayers")),active_layer=Api(Member(Member(variant,"LocalAdjustmentsLayers"),"ActiveLayer")),first_layer=Api(Items(Member(variant,"LocalAdjustmentsLayers")).FirstOrDefault()),color_balance=Api(Member(variant,"ColorBalance")),balance_model=Api(models.FirstOrDefault(o=>Name(o).Contains("ThreeWayColorBalanceViewModel"))),wheel_model=Api(models.FirstOrDefault(o=>Name(o).EndsWith("ColorBalanceWheelViewModel"))),settings=Api(Member(variant,"Settings")),image=Api(Member(variant,"Image")),corrections=Api(Member(variant,"BasicColorCorrection")),first_correction=Api((Member(variant,"BasicColorCorrection") as IEnumerable)?.Cast<object>().FirstOrDefault())};
            else if(command=="viewer")
            {RequireTarget(request,variant!,document!,false);data=new {viewer=ViewerGeometry()};}
            else if(command=="read"&&request.TryGetProperty("scope",out var inventoryScope)&&inventoryScope.GetString()=="images")
            {if(document==null)throw new InvalidOperationException("no document");data=ImageInventory(document,request.GetProperty("options"));}
            else if(command=="read"&&request.TryGetProperty("scope",out var previewScope)&&previewScope.GetString()!.StartsWith("preview_"))
            {data=Preview(document!,variant!,request);}
            else if(command=="context")
            {
                if(variant==null||document==null)throw new InvalidOperationException("no selected native photo");
                var image=Member(variant,"Image");var variants=Items(Member(image,"Variants")).ToList();int ordinal=variants.FindIndex(v=>Member(v,"UUID")?.ToString()==Member(variant,"UUID")?.ToString())+1;
                string label=Member(image,"FileName")?.ToString()??"";if(ordinal>1)label+=" ["+ordinal+"]";
                data=new {target=Target(variant,document),image_path=Member(image,"FullpathFilename")?.ToString(),variant_label=label,layer_uuid=LayerId(variant),layers=Layers(variant),selected_count=Member(Member(Selection(document),"SelectedVariants"),"Count"),collection_uuid=Member(CurrentCollection(document),"UUID")?.ToString(),processing_engine=Convert.ToInt32(Member(variant,"ProcessingEngine")),process_revision=ProcessRevision(variant)};
            }
            else if(command=="layer")
            {
                RequireTarget(request,variant!,document!,true);
                string action=request.GetProperty("action").GetString()!;
                if(!request.TryGetProperty("before_layers",out var inventory)||!Equal(inventory,JsonSerializer.SerializeToElement(Layers(variant!))))throw new InvalidOperationException("native layer inventory changed");
                if(request.TryGetProperty("before_mask",out var beforeMask)&&!Equal(beforeMask,JsonSerializer.SerializeToElement(MaskState(variant!))))throw new InvalidOperationException("native mask changed");
                var collection=Member(variant,"LocalAdjustmentsLayers")!;
                var patch=request.TryGetProperty("patch",out var suppliedPatch)?suppliedPatch:request;
                if(action=="select")
                {
                    string id=request.GetProperty("layer_id").GetString()!;
                    int index=id=="Background"?-1:Items(collection).ToList().FindIndex(l=>IdOfLayer(l)==id);
                    if(id!="Background"&&index<0)throw new InvalidOperationException("native layer not found");
                    dispatched=true;Property(collection,"SelectedLayerIndex").SetValue(collection,index);
                }
                else if(action=="create_empty")
                {
                    dispatched=true;var created=Invoke(collection,"AddNew",variant,"C1 AI "+Guid.NewGuid().ToString("N").Substring(0,6),Member(document,"MessageBus"));
                    int index=Items(collection).ToList().FindIndex(l=>ReferenceEquals(l,created));
                    if(index>=0)Property(collection,"SelectedLayerIndex").SetValue(collection,index);
                }
                else if(action=="create_subject"||action=="create_background")
                {
                    if(maskManager==null||!ReferenceEquals(maskDocument,document))
                    {maskManager=Activator.CreateInstance(NativeType("P1.C1.Components.LocalAdjustmentsMasks.AutoMaskingManager"),new[]{document!});maskDocument=document;}
                    string name=action=="create_subject"?"SegmentAndCreateSubjectMask":"SegmentAndCreateBackgroundMask";
                    var method=maskManager!.GetType().GetMethods(Flags).Single(m=>m.Name==name&&m.GetParameters().Length==2);
                    var type=method.GetParameters()[0].ParameterType;
                    var variants=Array.CreateInstance(type.IsArray?type.GetElementType()!:type.GetGenericArguments().Single(),1);variants.SetValue(variant,0);
                    dispatched=true;method.Invoke(maskManager,new[]{(object)variants,Member(document,"MessageBus")!});
                }
                else if(action=="remove_created")
                {
                    var layer=ActiveLayer(variant)??throw new InvalidOperationException("no native layer selected");
                    if(request.GetProperty("layer_id").GetString()!=LayerId(variant))throw new InvalidOperationException("native layer changed before removal");
                    dispatched=true;Invoke(collection,"Remove",layer,Member(variant,"CoreImage"));
                }
                else
                {
                    var layer=ActiveLayer(variant)??throw new InvalidOperationException("select an adjustment layer");
                    if(Member(layer,"IsCloneOrHealLayer") is true)throw new InvalidOperationException("repair layer is not supported");
                    if(request.TryGetProperty("layer_id",out var requestedLayer)&&requestedLayer.GetString()!=IdOfLayer(layer))throw new InvalidOperationException("selected layer changed");
                    var mask=Member(layer,"Mask")!;var image=Member(variant,"CoreImage")!;
                    object? source=null;
                    if(patch.TryGetProperty("source_layer",out var sourceRef))
                    {
                        source=Items(collection).Single(l=>IdOfLayer(l)==sourceRef.GetProperty("native_id").GetString());
                        if(ReferenceEquals(source,layer)||Member(source,"IsCloneOrHealLayer") is true)throw new InvalidOperationException("choose a different adjustment layer as mask source");
                    }
                    if(action=="rename")
                    {
                        string name=patch.GetProperty("name").GetString()!;
                        if(string.IsNullOrWhiteSpace(name)||name.Length>128||name.Any(char.IsControl))throw new InvalidOperationException("invalid layer name");
                        dispatched=true;Property(layer,"Name").SetValue(layer,name);
                    }
                    else if(action=="set_enabled") {bool enabled=patch.GetProperty("enabled").GetBoolean();dispatched=true;Property(layer,"LayerEnabled").SetValue(layer,enabled);}
                    else if(action=="duplicate")
                    {
                        if(Invoke(collection,"CanMoreLayersBeAdded",1) is not true)throw new InvalidOperationException("C1 layer limit reached");
                        string name=patch.TryGetProperty("name",out var nameValue)?nameValue.GetString()!:"C1 AI copy "+Guid.NewGuid().ToString("N").Substring(0,6);
                        if(string.IsNullOrWhiteSpace(name)||name.Length>128||name.Any(char.IsControl))throw new InvalidOperationException("invalid layer name");
                        dispatched=true;var created=Invoke(layer,"DuplicateLayer",Member(variant,"UndoManager"),name)!;
                        Invoke(collection,"Add",created,image,Member(document,"MessageBus"));
                        Property(collection,"SelectedLayerIndex").SetValue(collection,Items(collection).ToList().FindIndex(l=>ReferenceEquals(l,created)));
                    }
                    else if(action=="move")
                    {
                        int index=patch.GetProperty("index").GetInt32();var all=Items(collection).ToList();
                        if(index<0||index>=all.Count)throw new InvalidOperationException("layer index outside current inventory");
                        dispatched=true;Invoke(collection,"Move",all.IndexOf(layer),index,image);
                        Property(collection,"SelectedLayerIndex").SetValue(collection,index);
                    }
                    else if(action=="invert") {dispatched=true;Invoke(layer,"InvertMask",variant);}
                    else if(action=="fill") {float density=(float)Number(patch,"density",0,1);dispatched=true;Invoke(layer,"FillMask",variant,density,new Func<bool>(()=>true));}
                    else if(action=="clear") {dispatched=true;Invoke(layer,"ClearMask",variant,Member(document,"MessageBus"));}
                    else if(action=="copy")
                    {
                        if(source==null)throw new InvalidOperationException("mask source missing");
                        var clone=Static("P1.C1.Core.LocalAdjustments.LocalAdjustmentsMask","CloneHandleAndCreateMaskWithMetadata",Invoke(Member(source,"Mask")!,"GetHandle")!);
                        dispatched=true;ChangeMask(layer,variant!,"C1 AI copy mask",_=>Property(layer,"Mask").SetValue(layer,clone));
                        Invoke(layer,"UpdateSelectionPointAfterCopyFromAdjustmentLayer",source);
                    }
                    else if(action=="combine")
                    {
                        if(source==null)throw new InvalidOperationException("mask source missing");
                        string blend=patch.GetProperty("mode").GetString()! switch {"add"=>"bmDraw","subtract"=>"bmErase","intersect"=>"bmMultiply",_=>throw new InvalidOperationException("unsupported mask blend")};
                        var sourceMask=Member(source,"Mask")!;var handle=Invoke(sourceMask,"GetHandle")!;
                        // C1 copies the source handle into a new native submask; it does not remove the source layer.
                        string subtype=Invoke(sourceMask,"GetSubmaskType",handle)?.ToString()??"Brush";
                        dispatched=true;AddMask(layer,handle,subtype,blend,variant!,Member(sourceMask,"GeneratedMaskContents"),Member(sourceMask,"SkinMaskOptions"));
                    }
                    else if(action=="feather")
                    {
                        float radius=(float)Number(patch,"radius",0,300);
                        dispatched=true;
                        if(Invoke(layer,"CreateFeatherMaskUndo",image) is not true)throw new InvalidOperationException("C1 could not prepare feathering");
                        Invoke(layer,"ApplyFeatherMask",image,radius);
                    }
                    else if(action=="linear_gradient")
                    {
                        var dimensions=(System.Drawing.Size)Member(variant,"ImageDimensions")!;
                        object Point(string key) {var a=patch.GetProperty(key).EnumerateArray().Select(x=>x.GetDouble()).ToArray();if(a.Length!=2||a.Any(x=>!double.IsFinite(x)||x<0||x>1))throw new InvalidOperationException("normalized gradient coordinates required");return Activator.CreateInstance(NativeType("P1.C1.ImageCoreWrapper.CoordinateSystem.RawCoordinate"),a[0]*dimensions.Width,a[1]*dimensions.Height)!;}
                        var start=Point("start");var stop=Point("stop");float midpoint=(float)Number(patch,"midpoint",0.01,0.99);
                        if(start.Equals(stop))throw new InvalidOperationException("gradient endpoints must differ");
                        dispatched=true;ChangeMask(layer,variant!,"C1 AI linear gradient",m=>Invoke(m,"DrawLinearGradient",start,stop,null,midpoint));
                    }
                    else if(action=="radial_gradient")
                    {
                        var dimensions=(System.Drawing.Size)Member(variant,"ImageDimensions")!;
                        var center=patch.GetProperty("center").EnumerateArray().Select(x=>x.GetDouble()).ToArray();
                        if(center.Length!=2||center.Any(x=>!double.IsFinite(x)||x<0||x>1))throw new InvalidOperationException("normalized center required");
                        double rx=Number(patch,"radius_x",0.001,1)*dimensions.Width,ry=Number(patch,"radius_y",0.001,1)*dimensions.Height;
                        var radial=Activator.CreateInstance(NativeType("P1.C1.Core.LocalAdjustments.Wrapper.Types.CoreRadialGradientMask"),new System.Windows.Point(center[0]*dimensions.Width,center[1]*dimensions.Height),rx,ry,
                            EnumValue("P1.C1.Core.LocalAdjustments.Wrapper.Types.CoreParametricMaskFillOptions",patch.GetProperty("inverted").GetBoolean()?"CenterFromNoneToFull":"CenterFromFullToNone"),(float)Number(patch,"falloff",0,1),
                            Activator.CreateInstance(NativeType("P1.C1.Framework.Mathematics.AngleInDegrees"),Number(patch,"angle",-360,360)))!;
                        dispatched=true;ChangeMask(layer,variant!,"C1 AI radial gradient",m=>Invoke(m,"DrawRadialMask",radial,null));
                    }
                    else if(action=="luma_range")
                    {
                        var parameters=Activator.CreateInstance(NativeType("P1.C1.Core.LocalAdjustments.Mask.LuminosityParameter"),Number(patch,"shadow",0,255),Number(patch,"shadow_falloff",0,255),Number(patch,"highlight",0,255),Number(patch,"highlight_falloff",0,255),Number(patch,"radius",0,300),Number(patch,"sensitivity",0,100),patch.GetProperty("inverted").GetBoolean())!;
                        if(Member(parameters,"IsValid") is not true)throw new InvalidOperationException("invalid luma range ordering");
                        var handle=Static("P1.C1.Core.StylesSupport.StyleSetterMaskHelpers","CreateLumaMaskHandle",variant!,parameters);
                        var indices=Items(Member(layer,"SubmasksInformation")).Reverse().Select((s,i)=>(s,i)).Where(x=>Member(x.s,"IsLumaMask") is true).Select(x=>x.i).Reverse().ToArray();
                        dispatched=true;ChangeMask(layer,variant!,"C1 AI luma range",m=>{foreach(int i in indices)Invoke(m,"RemoveSubmask",i);AddMask(layer,handle,"Luma","bmMultiply",variant!);});
                    }
                    else if(action=="select_submask")
                    {
                        int index=patch.GetProperty("index").GetInt32();int count=Items(Member(layer,"SubmasksInformation")).Count();
                        if(index< -1||index>=count)throw new InvalidOperationException("submask index outside current inventory");
                        dispatched=true;Property(layer,"SelectedSubmaskIndex").SetValue(layer,index);
                    }
                    else throw new InvalidOperationException("unsupported native layer/mask command");
                }
                data=new {target=Target(variant,document),accepted=true,action};
            }
            else if(command=="module")
            {
                RequireTarget(request,variant!,document!,true);
                string module=request.GetProperty("module").GetString()!,action=request.GetProperty("action").GetString()!;
                var fields=ModuleFields(module);var before=request.GetProperty("before");
                foreach(string f in fields)if(!Equal(before.GetProperty(f),JsonSerializer.SerializeToElement(ReadField(variant!,f))))throw new InvalidOperationException("module values changed: "+f);
                if(action=="reset")
                {dispatched=true;Invoke(variant!,"ResetProperties",false,ModuleKeys(module));}
                else if(action=="paste")
                {
                    var source=request.GetProperty("values");var prepared=fields.ToDictionary(f=>f,f=>f=="advanced_mixer"||f=="skin_mixer"?DecodeMixerSnapshot(variant!,f,source.GetProperty(f)):Encode(variant!,f,source.GetProperty(f)));
                    dispatched=true;
                    foreach(string f in fields)
                        if(f=="advanced_mixer"||f=="skin_mixer")
                        {SetParameter(variant!,Mixers[f],prepared[f]);SetParameter(variant!,f=="skin_mixer"?"SelectedSkinColorCorrection":"SelectedAdvancedColorCorrection",source.GetProperty(f).GetProperty("selected_index").GetInt32());}
                        else SetField(variant!,f,prepared[f]);
                }
                else throw new InvalidOperationException("unsupported module action");
                data=new {target=Target(variant,document),layer_uuid=LayerId(variant),accepted=true,values=fields.ToDictionary(f=>f,f=>ReadField(variant!,f)),action};
            }
            else if(command=="clone")
            {
                RequireTarget(request,variant!,document!,false);
                var image=Member(variant,"Image")!;
                var before=Items(Member(image,"Variants")).Select(v=>Member(v,"UUID")!.ToString()!).ToHashSet();
                dispatched=true;
                var created=Invoke(image,"CloneVariant",Member(variant,"WrappedVariant")??variant,Member(variant,"ProcessingEngine"),false)??throw new InvalidOperationException("native clone returned no variant");
                var after=Items(Member(image,"Variants")).Select(v=>Member(v,"UUID")!.ToString()!).ToHashSet();
                string id=Member(created,"UUID")!.ToString()!;
                if(after.Count!=before.Count+1||before.Contains(id)||!before.IsSubsetOf(after)||!after.Contains(id))throw new InvalidOperationException("native clone UUID delta differs");
                SelectOnly(document!,created);
                data=new {target=Target(created,document),accepted=true,source_uuid=Member(variant,"UUID")?.ToString(),created_uuid=id,before_count=before.Count,after_count=after.Count};
            }
            else if(command=="select")
            {
                if(request.TryGetProperty("selected_uuids",out var expectedSelection))
                {
                    var expected=request.GetProperty("expected_target");
                    if(document==null||expected.GetProperty("document_uuid").GetString()!=Member(document,"UUID")?.ToString()||
                        !string.Equals(expected.GetProperty("document_path").GetString(),Member(document,"Path")?.ToString(),StringComparison.OrdinalIgnoreCase)||
                        ((IntPtr?)Member(document,"BoundWindowHandle"))!=System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle)throw new InvalidOperationException("native navigation document changed");
                    var current=Items(Member(Selection(document),"SelectedVariants")).Select(v=>Member(v,"UUID")!.ToString()).OrderBy(s=>s);
                    if(!current.SequenceEqual(expectedSelection.EnumerateArray().Select(s=>s.GetString()).OrderBy(s=>s)))throw new InvalidOperationException("native selection changed after listing");
                }
                else RequireTarget(request,variant!,document!,false);
                if(request.GetProperty("collection_uuid").GetString()!=Member(CurrentCollection(document!),"UUID")?.ToString())throw new InvalidOperationException("active collection changed");
                object chosen;
                if(request.TryGetProperty("direction",out var direction))
                {
                    var rows=CollectionVariants(document!,true);int index=Array.FindIndex(rows,v=>Member(v,"UUID")?.ToString()==Member(variant,"UUID")?.ToString());
                    int step=direction.GetString()=="next"?1:direction.GetString()=="previous"?-1:throw new InvalidOperationException("invalid navigation direction");
                    if(index<0||index+step<0||index+step>=rows.Length)throw new InvalidOperationException("no adjacent variant in the filtered collection");
                    chosen=rows[index+step];
                }
                else if(request.TryGetProperty("variant_uuid",out var uuid))chosen=CollectionVariants(document!,false).Single(v=>string.Equals(Member(v,"UUID")?.ToString(),uuid.GetString(),StringComparison.OrdinalIgnoreCase));
                else {long id=request.GetProperty("variant_id").GetInt64();chosen=CollectionVariants(document!,false).Single(v=>Convert.ToInt64(Member(v,"DatabaseId"))==id);}
                dispatched=true;SelectOnly(document!,chosen);
                data=new {target=Target(chosen,document),selected_uuid=Member(Member(Selection(document),"PrimaryVariant"),"UUID")?.ToString()};
            }
            else
            {
                RequireTarget(request,variant!,document!,command=="write");
                var fields=request.GetProperty("fields").EnumerateArray().Select(x=>x.GetString()!).ToArray();
                if(!request.GetProperty("expected_target").TryGetProperty("layer_uuid",out _))throw new InvalidOperationException("explicit native layer identity required");
                var values=fields.ToDictionary(f=>f,f=>ReadField(variant!,f));
                if(command=="write")
                {
                    var patch=request.GetProperty("patch");var expected=request.GetProperty("before");
                    var readback=request.TryGetProperty("readback_fields",out var wantedReadback)?wantedReadback.EnumerateArray().Select(x=>x.GetString()!).ToArray():fields;
                    foreach(string f in readback)ReadField(variant!,f);
                    if(!patch.EnumerateObject().Select(p=>p.Name).OrderBy(x=>x).SequenceEqual(fields.OrderBy(x=>x)))throw new InvalidOperationException("patch and fields differ");
                    foreach(string f in fields)if(!Equal(expected.GetProperty(f),JsonSerializer.SerializeToElement(values[f])))throw new InvalidOperationException("native before value changed: "+f);
                    var prepared=fields.ToDictionary(f=>f,f=>Encode(variant!,f,patch.GetProperty(f)));
                    try {dispatched=true;foreach(string f in fields)SetField(variant!,f,prepared[f]);}
                    catch(Exception e){return JsonSerializer.Serialize(new {status="outcome_unknown",error=new {code="NATIVE_SETTER_FAILED",message=(e.InnerException??e).ToString()},dispatched=true,replay_allowed=false});}
                    values=readback.ToDictionary(f=>f,f=>ReadField(variant!,f));
                }
                data=new {target=Target(variant,document),layer_uuid=LayerId(variant),values,selected_count=Convert.ToInt32(Member(Member(Selection(document),"SelectedVariants"),"Count"))};
            }
            return JsonSerializer.Serialize(new {status="ok",backend="c1_internal_model",read_only=command=="read"||command=="inspect"||command=="context",data});
        }
        catch(Exception e){return JsonSerializer.Serialize(new {status=dispatched?"outcome_unknown":"refused",error=new {code="INTERNAL_MODEL_ERROR",message=(e.InnerException??e).ToString()},dispatched,replay_allowed=false});}
    }
}
