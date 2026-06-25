using System.Reflection;
using System.Runtime.InteropServices;

// Probe v5: vuelca los tipos auxiliares necesarios para locate:
// PnPRowIdArray, PpObjectIdArray, PpObjectId, ProjectPartCollection.

const string PLNT3D = @"C:\Program Files\Autodesk\AutoCAD 2026\PLNT3D";
const string ACAD = @"C:\Program Files\Autodesk\AutoCAD 2026";
string outPath = @"C:\Users\aromera\AppData\Local\Temp\claude\C--Users-aromera-OneDrive---INGENIERIA-Y-DISE-O-ESTRUCTURAL-AVANZADO--S-L-AutocadMCP\8cbacb7a-bf32-40ce-9127-a351394118f4\scratchpad\api_probe5.txt";

var byName = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
void AddDir(string dir)
{
    if (!Directory.Exists(dir)) return;
    foreach (var f in Directory.GetFiles(dir, "*.dll"))
        if (!byName.ContainsKey(Path.GetFileName(f))) byName[Path.GetFileName(f)] = f;
}
AddDir(RuntimeEnvironment.GetRuntimeDirectory());
foreach (var shared in new[] { "Microsoft.WindowsDesktop.App", "Microsoft.NETCore.App" })
{
    var b = Path.Combine(@"C:\Program Files\dotnet\shared", shared);
    if (Directory.Exists(b))
    {
        var v8 = Directory.GetDirectories(b).Where(d => Path.GetFileName(d).StartsWith("8.")).OrderByDescending(d => d).FirstOrDefault();
        if (v8 != null) AddDir(v8);
    }
}
AddDir(ACAD);
AddDir(PLNT3D);

var resolver = new PathAssemblyResolver(byName.Values);
using var mlc = new MetadataLoadContext(resolver, coreAssemblyName: "System.Private.CoreLib");
using var w = new StreamWriter(outPath, false, System.Text.Encoding.UTF8);
void Log(string s) { Console.WriteLine(s); w.WriteLine(s); }

static string TN(Type t)
{
    if (t.IsByRef) return TN(t.GetElementType()!) + "&";
    if (t.IsGenericType) return $"{t.Name.Split('`')[0]}<{string.Join(", ", t.GetGenericArguments().Select(TN))}>";
    return t.Name;
}
static string Pars(ParameterInfo[] ps) => string.Join(", ", ps.Select(p => $"{TN(p.ParameterType)} {p.Name}"));

void DumpType(Type? t)
{
    if (t == null) { Log("   (null)"); return; }
    Log($"\n===== {t.FullName}   [asm:{t.Assembly.GetName().Name}]  base:{t.BaseType?.Name}  ifaces:[{string.Join(",", t.GetInterfaces().Select(i => i.Name))}]");
    foreach (var c in t.GetConstructors(BindingFlags.Public | BindingFlags.Instance))
        try { Log($"   .ctor({Pars(c.GetParameters())})"); } catch { }
    foreach (var p in t.GetProperties(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static))
        try { Log($"   PROP {TN(p.PropertyType)} {p.Name} [{string.Join("", p.GetIndexParameters().Select(ip => TN(ip.ParameterType)))}]{(p.GetAccessors(true).Any(a => a.IsStatic) ? " static" : "")}"); } catch { }
    foreach (var m in t.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static))
        try
        {
            if (m.IsSpecialName || m.DeclaringType?.FullName == "System.Object") continue;
            Log($"   {(m.IsStatic ? "static " : "")}{TN(m.ReturnType)} {m.Name}({Pars(m.GetParameters())})");
        }
        catch { }
}

// Buscar los tipos por nombre en PLNT3D Y en la raiz de AutoCAD (PnPDataLinks.dll vive ahi)
string[] wanted = { "PnPRowIdArray", "PpObjectIdArray", "PpObjectId", "ProjectPartCollection" };
var found = new HashSet<string>();
var scanDlls = Directory.GetFiles(PLNT3D, "*.dll").Concat(Directory.GetFiles(ACAD, "*.dll"));
foreach (var dll in scanDlls)
{
    Assembly a;
    try { a = mlc.LoadFromAssemblyPath(dll); } catch { continue; }
    Type[] ts;
    try { ts = a.GetTypes(); }
    catch (ReflectionTypeLoadException e) { ts = e.Types.Where(t => t != null).ToArray()!; }
    foreach (var t in ts.Where(t => t != null && t.IsPublic && wanted.Contains(t.Name)))
        if (found.Add(t!.FullName!)) DumpType(t);
}
w.Flush();
Log("\n[FIN] -> api_probe5.txt");
