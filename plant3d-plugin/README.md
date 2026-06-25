# PlantMcpDispatch — plugin .NET para AutoCAD Plant 3D 2026

Plugin C# que expone el comando **`MCPPLANTDISPATCH`** en AutoCAD 2026. Atiende
órdenes del servidor MCP (Python) mediante ficheros JSON en `C:\temp` (mismo
patrón IPC de ficheros que usa el dispatcher LISP, pero con su propio prefijo).

Sirve para operaciones que **requieren la API .NET de Plant 3D** y no son
accesibles desde SQLite ni AutoLISP (por ahora: localizar objetos en el dibujo
por su `PnPID`).

## Operaciones

| Comando | Parámetros | Payload de respuesta |
|---|---|---|
| `ping` | — | `{plugin, version, plant3d_available, project}` |
| `locate` | `{pnpids:[int], zoom:bool=true, select:bool=true}` | `{requested, found, not_found, found_count, dwg}` |

- `ping` valida que la DLL carga, el comando está registrado y el IPC responde.
- `locate` resuelve cada `PnPID` a su `ObjectId` en el DWG abierto vía el
  `DataLinksManager` del proyecto Plant 3D; opcionalmente selecciona los objetos
  y hace zoom a sus extents combinados. Los `PnPID` que no estén en el DWG
  actual se devuelven en `not_found` (es legítimo: pueden vivir en otro modelo).

## Contrato IPC

Directorio fijo: `C:\temp`.

- Fichero de comando (lo escribe Python):
  `C:\temp\autocad_mcp_plant_cmd_{request_id}.json`
  ```json
  { "request_id": "<hex>", "command": "ping|locate", "params": { }, "ts": 0 }
  ```
- Fichero de resultado (lo escribe el plugin, atómico tmp+rename, UTF-8):
  `C:\temp\autocad_mcp_plant_result_{request_id}.json`
  ```json
  { "request_id": "<id>", "ok": true,  "payload": { } }
  { "request_id": "<id>", "ok": false, "error": "<mensaje>" }
  ```

El plugin localiza el primer fichero de comando pendiente, lo despacha por un
**whitelist** (sin eval), escribe el resultado y borra el comando. Toda la
ejecución va en `try/catch` global: ante cualquier fallo escribe `ok:false`, de
modo que el lado Python (que hace polling con timeout) siempre recibe respuesta.

## Compilar

Requiere el SDK de .NET (8 o 9; el proyecto apunta a `net8.0-windows`) y AutoCAD
Plant 3D 2026 instalado en la ruta estándar (las referencias usan rutas
absolutas a `C:\Program Files\Autodesk\AutoCAD 2026\` y su carpeta `PLNT3D\`).

```powershell
cd plant3d-plugin
dotnet build PlantMcpDispatch.csproj -c Release
```

La salida es `plant3d-plugin\bin\Release\PlantMcpDispatch.dll`. Las DLLs de
Autodesk se referencian con `Private=false`: **no se copian** al output (AutoCAD
las resuelve en runtime).

## Cargar en AutoCAD

1. Abre AutoCAD Plant 3D 2026 con un proyecto y el modelo 3D donde quieras operar.
2. Teclea `NETLOAD` y selecciona `PlantMcpDispatch.dll`.
3. El comando queda disponible: se invoca **tecleando `MCPPLANTDISPATCH`** y Enter
   (sin paréntesis; no es un comando LISP).

Para carga automática al iniciar, registra la DLL en el mecanismo habitual de
AutoCAD (p. ej. la entrada de registro de aplicaciones de demanda o un
`acad.lsp`/bundle que ejecute `NETLOAD`). El servidor MCP envía el nombre del
comando a la línea de comandos igual que hace con el dispatcher LISP.

## Estructura

```
plant3d-plugin/
  PlantMcpDispatch.csproj      proyecto del plugin (SDK-style, net8.0-windows)
  src/
    IpcContract.cs             tipos del contrato JSON (comando, resultado, payloads)
    IpcChannel.cs              localizar/leer comando, escribir resultado atómico, borrar
    Plant3dAccess.cs           acceso aislado a la API Plant 3D (ping, locate)
    DispatchCommand.cs         comando MCPPLANTDISPATCH + dispatcher whitelist
  probe/                       utilidad aparte de descubrimiento de API (no se carga en AutoCAD)
```
