using System;
using System.Collections.Generic;
using Autodesk.AutoCAD.DatabaseServices;

// Tipos Plant 3D descubiertos por el probe de metadatos:
//   PlantApplication     -> Autodesk.ProcessPower.PlantInstance      (PnPProjectManagerMgd.dll)
//   PlantProject/Project -> Autodesk.ProcessPower.ProjectManager     (PnPProjectManagerMgd.dll)
//   DataLinksManager     -> Autodesk.ProcessPower.DataLinks          (PnPDataLinks.dll)
//   PpObjectId(Array)    -> Autodesk.ProcessPower.DataLinks          (PnPDataLinks.dll)
//   PnPRowIdArray        -> Autodesk.ProcessPower.DataObjects        (PnPDataObjects.dll)
using Autodesk.ProcessPower.PlantInstance;
using Autodesk.ProcessPower.ProjectManager;
using Autodesk.ProcessPower.DataLinks;
using Autodesk.ProcessPower.DataObjects;

namespace PlantMcpDispatch
{
    /// <summary>
    /// Capa de acceso a la API de Plant 3D. Aisla las llamadas a tipos Autodesk
    /// para que el dispatcher quede limpio y la incertidumbre de firmas este
    /// acotada y comentada.
    /// </summary>
    internal static class Plant3dAccess
    {
        /// <summary>
        /// Indica si la API de Plant 3D esta disponible (hay un proyecto abierto).
        /// No lanza: cualquier excepcion se traduce a false.
        /// </summary>
        public static bool IsAvailable(out string? projectName)
        {
            projectName = null;
            try
            {
                // PlantApplication.CurrentProject es estatico y devuelve PlantProject
                // (o null si no hay proyecto abierto en la sesion).
                PlantProject? prj = PlantApplication.CurrentProject;
                if (prj == null)
                    return false;
                projectName = prj.Name;
                return true;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Resuelve, para el conjunto de PnPID dados, sus ObjectId en el DWG
        /// ACTUALMENTE ABIERTO. Devuelve la lista de ObjectId encontrados y
        /// rellena <paramref name="found"/> / <paramref name="notFound"/>.
        ///
        /// Cadena de resolucion (API real descubierta):
        ///   PnPID (= rowid del SQLite)
        ///     -> PnPRowIdArray { rowid }
        ///     -> DataLinksManager.SelectAcPpObjectIds(rowIds) : PpObjectIdArray
        ///     -> por cada PpObjectId: DataLinksManager.MakeAcDbObjectIds(ppId) : ObjectIdCollection
        ///   Los ObjectId devueltos pueden pertenecer a un DWG de modelo distinto
        ///   del actualmente abierto (recorremos TODOS los DataLinksManager del
        ///   proyecto). Se filtran por <paramref name="activeDb"/>: los ObjectId
        ///   cuya base no sea la del DWG abierto se descartan, de modo que ese
        ///   PnPID acaba en not_found (existe en el proyecto, pero no en este DWG).
        ///
        /// Tolerante: cualquier fallo por un PnPID concreto lo manda a not_found
        /// sin tumbar la operacion completa.
        ///
        /// NOTA: el comportamiento exacto del filtrado por Database sigue PENDIENTE
        /// DE VALIDAR en AutoCAD vivo (se asume que id.Database referencia la base
        /// del DWG donde reside el objeto; confirmar con un proyecto multi-DWG).
        /// </summary>
        public static List<ObjectId> Locate(
            IEnumerable<int> pnpids,
            Database activeDb,
            out List<int> found,
            out List<int> notFound)
        {
            found = new List<int>();
            notFound = new List<int>();
            var objectIds = new List<ObjectId>();

            var prj = PlantApplication.CurrentProject
                ?? throw new InvalidOperationException("No hay proyecto Plant 3D abierto en la sesion.");

            // Reunir los DataLinksManager de todas las partes del proyecto
            // (Piping / PipingAndInstrumentation / etc.). Un PnPID puede estar
            // gestionado por cualquiera de ellas; probamos en todas.
            var managers = GetDataLinksManagers(prj);
            if (managers.Count == 0)
                throw new InvalidOperationException("El proyecto no expone ningun DataLinksManager.");

            foreach (int pnpid in pnpids)
            {
                bool located = false;
                try
                {
                    foreach (DataLinksManager dlm in managers)
                    {
                        if (TryLocateOne(dlm, pnpid, activeDb, objectIds))
                        {
                            located = true;
                            break; // ya encontrado en esta parte; no seguir
                        }
                    }
                }
                catch
                {
                    located = false; // cualquier fallo -> not_found, nunca propaga
                }

                if (located) found.Add(pnpid);
                else notFound.Add(pnpid);
            }

            return objectIds;
        }

        /// <summary>
        /// Intenta resolver un unico PnPID con un DataLinksManager concreto.
        /// Devuelve true y agrega los ObjectId VALIDOS que ademas pertenezcan al
        /// DWG ACTUALMENTE ABIERTO (<paramref name="activeDb"/>). Los ObjectId de
        /// otra base (objeto en otro modelo del proyecto) se descartan -> not_found.
        /// </summary>
        private static bool TryLocateOne(DataLinksManager dlm, int pnpid, Database activeDb, List<ObjectId> sink)
        {
            // PnPRowIdArray admite .ctor(IEnumerable<int>) (descubierto por probe).
            var rowIds = new PnPRowIdArray(new[] { pnpid });

            // SelectAcPpObjectIds(PnPRowIdArray) -> PpObjectIdArray (LinkedList<PpObjectId>)
            // TODO verificar en AutoCAD vivo: comportamiento cuando el rowid no
            // pertenece a esta parte (se espera coleccion vacia, no excepcion).
            PpObjectIdArray ppIds = dlm.SelectAcPpObjectIds(rowIds);
            if (ppIds == null || ppIds.Count == 0)
                return false;

            bool any = false;
            foreach (PpObjectId ppId in ppIds)
            {
                ObjectIdCollection acIds;
                try
                {
                    // MakeAcDbObjectIds(PpObjectId) -> ObjectIdCollection de AutoCAD
                    // (vacia si el objeto no esta en el dibujo actual).
                    acIds = dlm.MakeAcDbObjectIds(ppId);
                }
                catch
                {
                    continue; // este PpObjectId no resuelve en este dwg
                }

                if (acIds == null) continue;
                foreach (ObjectId id in acIds)
                {
                    if (id.IsNull || !id.IsValid || id.IsErased)
                        continue;

                    // Filtrar ObjectId que NO pertenezcan al DWG abierto: un PnPID
                    // puede resolver a un objeto de otro modelo del proyecto, cuyo
                    // ObjectId vive en otra Database y romperia la seleccion/zoom
                    // sobre el documento activo. Solo aceptamos los del DWG actual.
                    // PENDIENTE DE VALIDAR en AutoCAD vivo (ver nota en Locate()).
                    if (activeDb != null && id.Database != activeDb)
                        continue;

                    sink.Add(id);
                    any = true;
                }
            }
            return any;
        }

        /// <summary>
        /// Resuelve el DataLinksManager de la parte P&ID del proyecto abierto.
        /// La parte P&ID es el Project cuyo tipo de parte es PnId (su PartName
        /// suele ser "PnId"). Devuelve null si no hay proyecto, no hay parte
        /// P&ID o no expone DataLinksManager. No lanza: es tolerante por diseno
        /// (el probe pnid_probe reporta pnid_part_found=false en ese caso).
        /// </summary>
        public static DataLinksManager? GetPnidDataLinksManager(out string? note)
        {
            note = null;
            PlantProject? prj;
            try
            {
                prj = PlantApplication.CurrentProject;
            }
            catch (System.Exception ex)
            {
                note = "No se pudo obtener el proyecto Plant 3D actual: " + ex.Message;
                return null;
            }

            if (prj == null)
            {
                note = "No hay proyecto Plant 3D abierto en la sesion.";
                return null;
            }

            try
            {
                ProjectPartCollection parts = prj.ProjectParts;
                if (parts == null)
                {
                    note = "El proyecto no expone ProjectParts.";
                    return null;
                }

                // Identificamos la parte P&ID por su PartName ("PnId"), comparado
                // sin distinguir mayusculas. Es la via mas estable entre versiones;
                // el tipo concreto (PnIdProject) vive en PnIdProjectPartsMgd y aqui
                // basta con el nombre de parte para no acoplar el probe a ese tipo.
                foreach (Project part in parts)
                {
                    string? partName = null;
                    try { partName = part.PartName; } catch { /* parte sin PartName util */ }

                    if (partName != null &&
                        partName.IndexOf("PnId", System.StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        try
                        {
                            DataLinksManager dlm = part.DataLinksManager;
                            if (dlm != null)
                                return dlm;
                            note = "La parte P&ID no expone DataLinksManager.";
                            return null;
                        }
                        catch (System.Exception ex)
                        {
                            note = "Fallo al obtener el DataLinksManager de la parte P&ID: " + ex.Message;
                            return null;
                        }
                    }
                }

                note = "El proyecto no contiene una parte P&ID (PnId).";
                return null;
            }
            catch (System.Exception ex)
            {
                note = "Fallo recorriendo las partes del proyecto: " + ex.Message;
                return null;
            }
        }

        /// <summary>
        /// Obtiene los DataLinksManager de las partes del proyecto.
        /// </summary>
        private static List<DataLinksManager> GetDataLinksManagers(PlantProject prj)
        {
            var result = new List<DataLinksManager>();

            // Via robusta: recorrer las partes (Project) y leer su DataLinksManager.
            // ProjectParts es un ProjectPartCollection enumerable de Project.
            try
            {
                ProjectPartCollection parts = prj.ProjectParts;
                if (parts != null)
                {
                    foreach (Project part in parts)
                    {
                        try
                        {
                            DataLinksManager dlm = part.DataLinksManager;
                            if (dlm != null && !result.Contains(dlm))
                                result.Add(dlm);
                        }
                        catch
                        {
                            // parte sin DataLinksManager util; ignorar
                        }
                    }
                }
            }
            catch
            {
                // Fallback: la coleccion DataLinksManagers del proyecto.
                // TODO verificar firma de iteracion en AutoCAD vivo.
                try
                {
                    DataLinksManagerCollection coll = prj.DataLinksManagers;
                    if (coll != null)
                    {
                        foreach (DataLinksManager dlm in coll)
                            if (dlm != null && !result.Contains(dlm))
                                result.Add(dlm);
                    }
                }
                catch
                {
                    // sin managers accesibles
                }
            }

            return result;
        }
    }
}
