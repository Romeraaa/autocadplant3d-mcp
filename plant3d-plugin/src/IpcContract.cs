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
    }
}
