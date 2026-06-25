using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

[assembly: CommandClass(typeof(PlantMcpDispatch.DispatchCommand))]

namespace PlantMcpDispatch
{
    /// <summary>
    /// Comando AutoCAD <c>MCPPLANTDISPATCH</c>: atiende UNA orden IPC pendiente.
    /// El servidor Python escribe el fichero de comando y luego envia el nombre
    /// del comando a la linea de comandos; este metodo se ejecuta, escribe el
    /// resultado y borra el comando. Toda la ejecucion va en try/catch global
    /// para no dejar nunca a AutoCAD colgado ni sin responder.
    /// </summary>
    public sealed class DispatchCommand
    {
        [CommandMethod("MCPPLANTDISPATCH", CommandFlags.Modal)]
        public void Dispatch()
        {
            string? cmdPath = null;
            string requestId = "unknown";

            try
            {
                cmdPath = IpcChannel.FindPendingCommandFile();
                if (cmdPath == null)
                {
                    // No hay nada pendiente: nada que hacer (no es un error IPC).
                    return;
                }

                CommandFile cmd = IpcChannel.ReadCommand(cmdPath);
                requestId = cmd.RequestId;

                object payload = Execute(cmd);

                IpcChannel.WriteResult(new ResultFile
                {
                    RequestId = requestId,
                    Ok = true,
                    Payload = payload,
                });
            }
            catch (System.Exception ex)
            {
                // Cualquier fallo -> resultado ok:false. El lado Python hace
                // polling con timeout y necesita SIEMPRE un resultado.
                try
                {
                    IpcChannel.WriteResult(new ResultFile
                    {
                        RequestId = requestId,
                        Ok = false,
                        Error = ex.Message,
                    });
                }
                catch
                {
                    // Si ni siquiera podemos escribir el error, no hay mas que hacer.
                }
            }
            finally
            {
                if (cmdPath != null)
                    IpcChannel.DeleteCommandFile(cmdPath);
            }
        }

        /// <summary>
        /// Despacha por nombre de comando con WHITELIST (switch). Nunca eval.
        /// </summary>
        private static object Execute(CommandFile cmd)
        {
            switch (cmd.Command)
            {
                case "ping":
                    return DoPing();
                case "locate":
                    return DoLocate(cmd.Params);
                default:
                    throw new InvalidOperationException($"Comando no soportado: '{cmd.Command}'.");
            }
        }

        // ---------------------------------------------------------------- ping
        private static PingPayload DoPing()
        {
            bool available = Plant3dAccess.IsAvailable(out string? project);
            return new PingPayload
            {
                // Plugin tiene su valor por defecto ("PlantMcpDispatch") como
                // unica fuente de verdad en IpcContract.PingPayload; no se reasigna.
                Version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0.0.0",
                Plant3dAvailable = available,
                Project = available ? project : null,
            };
        }

        // -------------------------------------------------------------- locate
        private static LocatePayload DoLocate(JsonElement prms)
        {
            // Contrato IPC nuevo (resolucion por HANDLE; la API de Plant 3D no
            // resuelve en AutoCAD vivo -> devuelve 0 encontrados):
            //   { targets:[ {pnpid:int, dwg:string, handle:long} ],
            //     pnpids:[int], zoom:bool=true, select:bool=true }
            // 'handle' es el valor decimal (Int64) del handle de AutoCAD ya
            // combinado high/low por Python (p.ej. 9390 == hex 24AE).
            var pnpids = new List<int>();
            var targets = new List<LocateTarget>();
            bool zoom = true;
            bool select = true;

            if (prms.ValueKind == JsonValueKind.Object)
            {
                if (prms.TryGetProperty("pnpids", out JsonElement arr) && arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement e in arr.EnumerateArray())
                    {
                        if (e.ValueKind == JsonValueKind.Number && e.TryGetInt32(out int v))
                            pnpids.Add(v);
                        else if (e.ValueKind == JsonValueKind.String && int.TryParse(e.GetString(), out int sv))
                            pnpids.Add(sv);
                    }
                }
                // targets: una entrada por (pnpid, dwg, handle). Parseo manual
                // con JsonElement, igual que el resto de parametros.
                if (prms.TryGetProperty("targets", out JsonElement tgt) && tgt.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement t in tgt.EnumerateArray())
                    {
                        if (t.ValueKind != JsonValueKind.Object) continue;

                        int pnpid = 0;
                        if (t.TryGetProperty("pnpid", out JsonElement pe))
                        {
                            if (pe.ValueKind == JsonValueKind.Number) pe.TryGetInt32(out pnpid);
                            else if (pe.ValueKind == JsonValueKind.String) int.TryParse(pe.GetString(), out pnpid);
                        }

                        string? dwg = null;
                        if (t.TryGetProperty("dwg", out JsonElement de) && de.ValueKind == JsonValueKind.String)
                            dwg = de.GetString();

                        long handle = 0;
                        bool hasHandle = false;
                        if (t.TryGetProperty("handle", out JsonElement he))
                        {
                            if (he.ValueKind == JsonValueKind.Number && he.TryGetInt64(out handle)) hasHandle = true;
                            else if (he.ValueKind == JsonValueKind.String && long.TryParse(he.GetString(), out handle)) hasHandle = true;
                        }

                        if (hasHandle && handle != 0)
                            targets.Add(new LocateTarget { Pnpid = pnpid, Dwg = dwg, Handle = handle });
                    }
                }
                if (prms.TryGetProperty("zoom", out JsonElement z) &&
                    (z.ValueKind == JsonValueKind.True || z.ValueKind == JsonValueKind.False))
                    zoom = z.GetBoolean();
                if (prms.TryGetProperty("select", out JsonElement s) &&
                    (s.ValueKind == JsonValueKind.True || s.ValueKind == JsonValueKind.False))
                    select = s.GetBoolean();
            }

            Document doc = AcadApp.DocumentManager.MdiActiveDocument
                ?? throw new InvalidOperationException("No hay documento activo en AutoCAD.");

            // 'requested' cuenta los PnPID distintos solicitados. Si no llegan
            // 'pnpids' pero si 'targets', derivamos los distintos de los targets.
            var requestedPnpids = new HashSet<int>(pnpids);
            foreach (LocateTarget tt in targets)
                requestedPnpids.Add(tt.Pnpid);

            var payload = new LocatePayload
            {
                Requested = requestedPnpids.Count,
                Dwg = doc.Name,
            };

            // --- Ruta principal: resolucion por HANDLE -------------------------
            if (targets.Count > 0)
            {
                string activeDwg = System.IO.Path.GetFileName(doc.Name) ?? "";
                Database db = doc.Database;

                var objectIdsH = new List<ObjectId>();
                var foundSet = new HashSet<int>();

                foreach (LocateTarget t in targets)
                {
                    // Solo resolvemos handles del DWG actualmente abierto: el handle
                    // es relativo a su Database. Comparacion por basename, sin mayus.
                    string tDwg = System.IO.Path.GetFileName(t.Dwg ?? "") ?? "";
                    if (!string.Equals(tDwg, activeDwg, StringComparison.OrdinalIgnoreCase))
                        continue;

                    if (TryGetIdByHandle(db, t.Handle, out ObjectId id) &&
                        !id.IsNull && id.IsValid && !id.IsErased)
                    {
                        objectIdsH.Add(id);
                        foundSet.Add(t.Pnpid);
                    }
                }

                payload.Found = new List<int>(foundSet);
                payload.NotFound = new List<int>();
                foreach (int p in requestedPnpids)
                    if (!foundSet.Contains(p)) payload.NotFound.Add(p);
                payload.FoundCount = payload.Found.Count;

                if (objectIdsH.Count == 0)
                    return payload;

                ApplySelectionAndZoom(doc, objectIdsH, select, zoom);
                return payload;
            }

            // --- Fallback: cliente viejo sin 'targets' -> ruta Plant 3D ---------
            if (pnpids.Count == 0)
                return payload; // nada que localizar

            // Resolver PnPID -> ObjectId via Plant 3D (operacion tolerante).
            // Pasamos la Database del documento activo para que Plant3dAccess
            // descarte ObjectId que pertenezcan a otro DWG de modelo del proyecto
            // (irian a not_found): la seleccion/zoom de abajo opera sobre 'doc'.
            List<ObjectId> objectIds = Plant3dAccess.Locate(
                pnpids, doc.Database, out List<int> found, out List<int> notFound);
            payload.Found = found;
            payload.NotFound = notFound;
            payload.FoundCount = found.Count;

            if (objectIds.Count == 0)
                return payload;

            ApplySelectionAndZoom(doc, objectIds, select, zoom);
            return payload;
        }

        /// <summary>
        /// Resuelve un handle (valor decimal Int64) a ObjectId en la Database
        /// dada. Usa <see cref="Database.TryGetObjectId"/> si esta disponible;
        /// si no, GetObjectId con try/catch. Devuelve false ante cualquier fallo.
        /// </summary>
        private static bool TryGetIdByHandle(Database db, long handle, out ObjectId id)
        {
            id = ObjectId.Null;
            try
            {
                // TryGetObjectId(Handle, out ObjectId) existe en acdbmgd 2026.
                return db.TryGetObjectId(new Handle(handle), out id) && !id.IsNull;
            }
            catch
            {
                // Alternativa por si la sobrecarga no estuviera disponible.
                try
                {
                    id = db.GetObjectId(false, new Handle(handle), 0);
                    return !id.IsNull;
                }
                catch
                {
                    id = ObjectId.Null;
                    return false;
                }
            }
        }

        /// <summary>
        /// Aplica seleccion implicita y/o zoom a los ObjectId resueltos, bajo
        /// DocumentLock. Best-effort: nunca tumba locate.
        /// </summary>
        private static void ApplySelectionAndZoom(Document doc, List<ObjectId> objectIds, bool select, bool zoom)
        {

            // Bloquear el documento para operar sobre el (estamos en contexto de comando).
            using (DocumentLock _ = doc.LockDocument())
            {
                Editor ed = doc.Editor;
                ObjectId[] idArray = objectIds.ToArray();

                // ZOOM primero, SELECCION despues: el comando ZOOM _Object consume
                // la seleccion que recibe y deja la linea de comandos limpia; si
                // dejaramos la seleccion implicita despues, queda visible al usuario.
                if (zoom)
                {
                    try
                    {
                        ZoomToObjects(ed, idArray);
                    }
                    catch
                    {
                        // El zoom es best-effort; no debe tumbar locate.
                    }
                }

                if (select)
                {
                    try
                    {
                        // Seleccion final visible (pinzamientos) sobre la pieza.
                        ed.SetImpliedSelection(idArray);
                    }
                    catch
                    {
                        // La seleccion implicita es best-effort; no debe tumbar locate.
                    }
                }
            }
        }

        /// <summary>
        /// Encadra los objetos con el comando nativo <c>ZOOM _Object</c>, que
        /// funciona en cualquier orientacion de camara (2D o vistas 3D oblicuas),
        /// a diferencia de fabricar una ViewTableRecord 2D a partir de extents.
        /// Se ejecuta en contexto de comando Modal con el DocumentLock ya tomado;
        /// Editor.Command es sincrono en AutoCAD 2026.
        /// </summary>
        private static void ZoomToObjects(Editor ed, ObjectId[] ids)
        {
            if (ids.Length == 0)
                return;

            try
            {
                // Via principal: pasar el SelectionSet directamente a ZOOM _Object.
                SelectionSet ss = SelectionSet.FromObjectIds(ids);
                ed.Command("_.ZOOM", "_Object", ss, "");
            }
            catch
            {
                // Alternativa: dejar la seleccion previa y referirla con _P
                // (Previous) en ZOOM _Object. Cualquier prompt residual se cancela
                // abajo para dejar la linea de comandos limpia.
                ed.SetImpliedSelection(ids);
                ed.Command("_.ZOOM", "_Object", "_P", "");
            }
        }
    }
}
