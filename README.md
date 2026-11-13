# Sound Studio

De un enlace de YouTube a un sonido de Minecraft listo para sonar en el juego.

1. Pega un enlace de YouTube y recorta el fragmento sobre la forma de onda.
2. Elige el destino: un resource pack (carpeta o `.zip`), el proyecto de un mod o un pack nuevo.
3. Ponle nombre (por ejemplo `pantheon.fountain.1`) y pulsa **Añadir**.

La app convierte el fragmento a `.ogg` (Vorbis) y lo guarda en `assets/<namespace>/sounds/…`.
También añade la entrada a `sounds.json` con `"stream": true` y, si escribes un subtítulo, lo pone en `lang/en_us.json`.
Al terminar te da el comando `/playsound` para probarlo. En el juego, pulsa `F3`+`T` para recargar los recursos.

## Requisitos

- Windows con Python 3 (con tkinter) y Microsoft Edge
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) y [ffmpeg](https://ffmpeg.org) en el `PATH`:

```bash
winget install yt-dlp
```

```bash
winget install ffmpeg
```

## Uso

Haz doble clic en `Sound Studio.pyw` (o en `start.bat`). La app se abre en su propia ventana y se cierra sola cuando cierras esa ventana.

Atajos: `Espacio` reproduce la selección, `I` / `O` marcan el inicio y el fin en el cursor, la rueda del ratón hace zoom en la forma de onda.

La caché de audio y la configuración se guardan en `%LOCALAPPDATA%\MCSoundStudio`.
