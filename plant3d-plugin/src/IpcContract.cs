using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PlantMcpDispatch
{
    /// <summary>
    /// Tipos del contrato IPC por ficheros JSON. Comparten "espiritu" con el
    /// dispatcher LISP pero usan su propio prefijo (autocad_mcp_plant_*) para no
    /// colisionar con el canal LISP. El servidor Python fija este contrato.
    /// </summary>
    internal sealed class CommandFile
    {
        [JsonPropertyName("request_id")]
        public string RequestId { get; set; } = "";

        [JsonPropertyName("command")]
        public string Command { get; set; } = "";

        // Parametros libres; se interpretan por comando. Puede venir ausente.
        [JsonPropertyName("params")]
        public JsonElement Params { get; set; }

        [JsonPropertyName("ts")]
        public double Ts { get; set; }
    }

    /// <summary>Resultado escrito por el plugin (forma ok:true / ok:false).</summary>
    internal sealed class ResultFile
    {
        [JsonPropertyName("request_id")]
        public string RequestId { get; set; } = "";

        [JsonPropertyName("ok")]
        public bool Ok { get; set; }

        // En exito: payload con datos. En error: ausente.
        [JsonPropertyName("payload")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public object? Payload { get; set; }

        // En error: mensaje. En exito: ausente.
        [JsonPropertyName("error")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? Error { get; set; }
    }

    /// <summary>Payload de la operacion <c>ping</c>.</summary>
    internal sealed class PingPayload
    {
        [JsonPropertyName("plugin")]
        public string Plugin { get; set; } = "PlantMcpDispatch";

        [JsonPropertyName("version")]
        public string Version { get; set; } = "";

        [JsonPropertyName("plant3d_available")]
        public bool Plant3dAvailable { get; set; }

        [JsonPropertyName("project")]
        public string? Project { get; set; }
    }

    /// <summary>
    /// Objetivo de localizacion del comando <c>locate</c> (contrato nuevo):
    /// una entrada por (pnpid, dwg, handle). Se rellena por parseo manual de
    /// JsonElement en DispatchCommand.DoLocate; no se deserializa directamente.
    /// </summary>
    internal sealed class LocateTarget
    {
        // PnPID (rowid del SQLite) al que pertenece el objeto.
        public int Pnpid { get; set; }

        // Basename del DWG donde vive el objeto (p.ej. "23099-PIP-MOD-0001_R9.dwg").
        public string? Dwg { get; set; }

        // Valor decimal (Int64) del handle de AutoCAD ya combinado high/low.
        public long Handle { get; set; }
    }

    /// <summary>Fila muestra del probe <c>pnid_probe</c>.</summary>
    internal sealed class PnidSampleRow
    {
        [JsonPropertyName("rowid")]
        public int RowId { get; set; }

        [JsonPropertyName("class")]
        public string Class { get; set; } = "";

        [JsonPropertyName("tag")]
        public string Tag { get; set; } = "";
    }

    /// <summary>Linea muestra del probe <c>pnid_probe</c>.</summary>
    internal sealed class PnidSampleLine
    {
        [JsonPropertyName("group_id")]
        public int GroupId { get; set; }

        [JsonPropertyName("line_number")]
        public string LineNumber { get; set; } = "";

        [JsonPropertyName("service")]
        public string Service { get; set; } = "";
    }

    /// <summary>
    /// Payload del probe de diagnostico <c>pnid_probe</c>: vuelca lo que se ha
    /// podido leer del P&ID activo. Tolerante: cualquier fallo parcial va a
    /// <c>notes</c> y el resto se rellena con lo disponible.
    /// </summary>
    internal sealed class PnidProbePayload
    {
        [JsonPropertyName("pnid_part_found")]
        public bool PnidPartFound { get; set; }

        [JsonPropertyName("dwg")]
        public string Dwg { get; set; } = "";

        [JsonPropertyName("row_count")]
        public int RowCount { get; set; }

        [JsonPropertyName("by_class")]
        public Dictionary<string, int> ByClass { get; set; } = new();

        [JsonPropertyName("sample_rows")]
        public List<PnidSampleRow> SampleRows { get; set; } = new();

        [JsonPropertyName("line_count")]
        public int LineCount { get; set; }

        [JsonPropertyName("sample_lines")]
        public List<PnidSampleLine> SampleLines { get; set; } = new();

        [JsonPropertyName("notes")]
        public List<string> Notes { get; set; } = new();
    }

    /// <summary>Payload de la operacion <c>locate</c>.</summary>
    internal sealed class LocatePayload
    {
        [JsonPropertyName("requested")]
        public int Requested { get; set; }

        [JsonPropertyName("found")]
        public List<int> Found { get; set; } = new();

        [JsonPropertyName("not_found")]
        public List<int> NotFound { get; set; } = new();

        [JsonPropertyName("found_count")]
        public int FoundCount { get; set; }

        [JsonPropertyName("dwg")]
        public string? Dwg { get; set; }

        // true si se aplico el aislado de objetos (ISOLATEOBJECTS) en este locate.
        [JsonPropertyName("isolated")]
        public bool Isolated { get; set; }
    }

    /// <summary>
    /// Payload del comando <c>unisolate</c>: revierte el aislado mostrando todo
    /// lo oculto (UNISOLATEOBJECTS). Best-effort: ok:true aunque no haya nada que
    /// revertir o falte documento activo; las incidencias van en notes.
    /// </summary>
    internal sealed class UnisolatePayload
    {
        [JsonPropertyName("dwg")]
        public string? Dwg { get; set; }

        [JsonPropertyName("ok")]
        public bool Ok { get; set; } = true;

        [JsonPropertyName("notes")]
        public List<string> Notes { get; set; } = new();
    }
}
