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
            // Parametros: { pnpids:[int], zoom:bool=true, select:bool=true }
            var pnpids = new List<int>();
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
                if (prms.TryGetProperty("zoom", out JsonElement z) &&
                    (z.ValueKind == JsonValueKind.True || z.ValueKind == JsonValueKind.False))
                    zoom = z.GetBoolean();
                if (prms.TryGetProperty("select", out JsonElement s) &&
                    (s.ValueKind == JsonValueKind.True || s.ValueKind == JsonValueKind.False))
                    select = s.GetBoolean();
            }

            Document doc = AcadApp.DocumentManager.MdiActiveDocument
                ?? throw new InvalidOperationException("No hay documento activo en AutoCAD.");

            var payload = new LocatePayload
            {
                Requested = pnpids.Count,
                Dwg = doc.Name,
            };

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

            // Bloquear el documento para operar sobre el (estamos en contexto de comando).
            using (DocumentLock _ = doc.LockDocument())
            {
                Editor ed = doc.Editor;

                if (select)
                {
                    try
                    {
                        ed.SetImpliedSelection(objectIds.ToArray());
                    }
                    catch
                    {
                        // La seleccion implicita es best-effort; no debe tumbar locate.
                    }
                }

                if (zoom)
                {
                    try
                    {
                        ZoomToObjects(doc, objectIds);
                    }
                    catch
                    {
                        // El zoom es best-effort.
                    }
                }
            }

            return payload;
        }

        /// <summary>
        /// Hace zoom a los extents combinados de los objetos indicados, sin
        /// abrir dialogos (API de vista directa).
        /// </summary>
        private static void ZoomToObjects(Document doc, List<ObjectId> ids)
        {
            Database db = doc.Database;
            Editor ed = doc.Editor;

            var ext = new Extents3d();
            bool hasExt = false;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                foreach (ObjectId id in ids)
                {
                    if (id.IsNull || id.IsErased) continue;
                    if (tr.GetObject(id, OpenMode.ForRead) is Entity ent)
                    {
                        try
                        {
                            Extents3d e = ent.GeometricExtents;
                            if (!hasExt) { ext = e; hasExt = true; }
                            else ext.AddExtents(e);
                        }
                        catch
                        {
                            // entidad sin extents geometricos; ignorar
                        }
                    }
                }
                tr.Commit();
            }

            if (!hasExt)
                return;

            // Construir y aplicar una ViewTableRecord centrada en los extents.
            using (ViewTableRecord view = ed.GetCurrentView())
            {
                Point3d min = ext.MinPoint;
                Point3d max = ext.MaxPoint;

                double width = max.X - min.X;
                double height = max.Y - min.Y;
                var center = new Point2d((min.X + max.X) / 2.0, (min.Y + max.Y) / 2.0);

                // Margen del 15% alrededor del conjunto.
                const double margin = 1.15;
                if (width <= 0) width = 1.0;
                if (height <= 0) height = 1.0;

                view.Width = width * margin;
                view.Height = height * margin;
                view.CenterPoint = center;

                ed.SetCurrentView(view);
            }
        }
    }
}
