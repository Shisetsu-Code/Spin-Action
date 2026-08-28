# Tester-Spin autoactualizable

## Objetivo

`Tester-Spin.exe` es un launcher estable. El usuario abre siempre el mismo archivo y el launcher prepara/actualiza la aplicación antes de iniciarla.

El código ejecutable de Tester-Spin y los datos persistentes están separados:

- launcher: `Tester-Spin.exe`
- runtime administrado: `%LOCALAPPDATA%\Tester-Spin\runtime\repo`
- entorno Python: `%LOCALAPPDATA%\Tester-Spin\venv`
- Chromium Playwright: `%LOCALAPPDATA%\Tester-Spin\playwright`
- configuración: `%LOCALAPPDATA%\Tester-Spin\updater.json`
- datos: ruta persistente guardada en `updater.json`

Si el launcher se construye dentro del checkout actual y detecta `data\tester-spin.sqlite3`, conserva esa carpeta como directorio de datos. Esto permite seguir usando, por ejemplo, `C:\Proyectos\Tester-Spin\data` aunque el runtime se actualice en `%LOCALAPPDATA%`.

## Canal Git privado (actual)

Configuración generada por defecto:

```json
{
  "mode": "git",
  "repo_url": "https://github.com/Shisetsu-Code/Tester-Spin.git",
  "branch": "main",
  "manifest_url": "",
  "data_dir": "C:\\Proyectos\\Tester-Spin\\data"
}
```

En cada apertura:

1. consulta `origin/main`;
2. si hay una versión nueva, sincroniza el checkout runtime administrado;
3. si cambia `requirements.txt`, actualiza el venv;
4. abre `run.py` usando el directorio de datos persistente;
5. si GitHub/red no están disponibles y ya existe runtime, abre la última versión instalada.

El checkout de desarrollo nunca se resetea: las operaciones destructivas de `git reset/clean` sólo ocurren sobre la copia administrada de `%LOCALAPPDATA%`.

El repositorio es privado, por lo que este modo usa las credenciales Git ya configuradas en Windows/Git Credential Manager. No se guarda ni incrusta un PAT dentro del EXE.

## Canal de distribución por manifest

Para máquinas que no deben tener acceso al repositorio privado, cambiar:

```json
{
  "mode": "manifest",
  "manifest_url": "https://updates.example.com/tester-spin/update.json"
}
```

Formato:

```json
{
  "schema": "tester-spin-update/v1",
  "version": "2026.08.28.1",
  "url": "https://updates.example.com/tester-spin/tester-spin-2026.08.28.1.zip",
  "sha256": "<64 hex>",
  "entrypoint": "run.py"
}
```

El launcher descarga primero a staging, verifica SHA-256, valida que el ZIP no pueda escribir fuera de su directorio (zip-slip), extrae a una carpeta de versión y recién entonces cambia el puntero `current.json`. Mantiene las tres versiones más recientes.

R2/Pages/CDN puede alojar tanto `update.json` como el ZIP. El launcher no necesita credenciales del repositorio en este modo.

## Construir el EXE estable

PowerShell 5.1:

```powershell
cd C:\Proyectos\Tester-Spin
.\scripts\build-launcher.ps1 -Clean
```

Resultado:

```text
dist\Tester-Spin.exe
```

Ese archivo se puede copiar al Escritorio o fijar a Inicio. En la primera ejecución crea su runtime administrado; las siguientes aperturas buscan actualizaciones automáticamente.

## Crear un paquete para R2/HTTP

Ejemplo:

```powershell
.\scripts\build-update-package.ps1 `
  -Version "2026.08.28.1" `
  -BaseUrl "https://updates.example.com/tester-spin"
```

Genera:

```text
release\tester-spin-2026.08.28.1.zip
release\update.json
```

Subir ambos archivos y configurar `manifest_url` una sola vez.

## Desarrollo

`Abrir-Tester-Spin.cmd` continúa ejecutando el checkout de desarrollo y no usa el runtime administrado. Esto evita que el auto-updater interfiera con ramas, cambios locales o debugging.
