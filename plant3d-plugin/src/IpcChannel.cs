using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace PlantMcpDispatch
{
    /// <summary>
    /// Canal IPC por ficheros: localiza el comando, parsea, y escribe el
    /// resultado de forma ATOMICA (tmp + rename) en UTF-8. Mismo directorio
    /// fijo C:/temp que usa el dispatcher LISP.
    /// </summary>
    internal static class IpcChannel
    {
        // Directorio IPC fijo (igual que mcp_dispatch.lsp).
        public const string IpcDir = @"C:\temp";

        // Prefijos propios del canal Plant (no colisionan con el canal LISP).
        public const string CmdPrefix = "autocad_mcp_plant_cmd_";
        public const string ResultPrefix = "autocad_mcp_plant_result_";

        // UTF-8 sin BOM (el lado Python espera UTF-8 plano).
        private static readonly UTF8Encoding Utf8NoBom = new(false);

        private static readonly JsonSerializerOptions JsonOpts = new()
        {
            // No escapar caracteres no-ASCII innecesariamente (rutas, acentos).
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        };

        /// <summary>
        /// Devuelve la ruta del PRIMER fichero de comando pendiente, o null si no hay.
        /// Orden por fecha de escritura para procesar el mas antiguo primero.
        /// </summary>
        public static string? FindPendingCommandFile()
        {
            if (!Directory.Exists(IpcDir))
                return null;

            return Directory.EnumerateFiles(IpcDir, CmdPrefix + "*.json")
                .OrderBy(p => File.GetLastWriteTimeUtc(p))
                .FirstOrDefault();
        }

        /// <summary>Lee y parsea el fichero de comando.</summary>
        public static CommandFile ReadCommand(string path)
        {
            string json = File.ReadAllText(path, Utf8NoBom);
            CommandFile? cmd = JsonSerializer.Deserialize<CommandFile>(json, JsonOpts);
            if (cmd == null)
                throw new InvalidDataException("El fichero de comando no contiene JSON valido.");
            if (string.IsNullOrEmpty(cmd.RequestId))
                throw new InvalidDataException("El comando carece de 'request_id'.");
            return cmd;
        }

        /// <summary>Escribe el resultado de forma atomica (tmp + File.Move overwrite).</summary>
        public static void WriteResult(ResultFile result)
        {
            Directory.CreateDirectory(IpcDir);
            string finalPath = Path.Combine(IpcDir, ResultPrefix + result.RequestId + ".json");
            string tmpPath = finalPath + ".tmp";

            string json = JsonSerializer.Serialize(result, JsonOpts);
            File.WriteAllText(tmpPath, json, Utf8NoBom);

            // Rename atomico; sobreescribe si por algun motivo ya existiera.
            if (File.Exists(finalPath))
                File.Delete(finalPath);
            File.Move(tmpPath, finalPath);
        }

        /// <summary>Borra el fichero de comando ya procesado (silencioso si falla).</summary>
        public static void DeleteCommandFile(string path)
        {
            try
            {
                if (File.Exists(path))
                    File.Delete(path);
            }
            catch
            {
                // No es critico: si no se puede borrar, el siguiente ciclo lo
                // reintentara; mejor eso que tumbar la operacion.
            }
        }
    }
}
