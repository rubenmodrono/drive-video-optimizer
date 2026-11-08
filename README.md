# Drive Video Optimizer

Recomprime los vídeos de tu Google Drive para que ocupen menos, sin perder lo que importa para subirlos a redes:

- 1080p como máximo, HEVC (H.265) a unos 10 Mbps, en 2 pasadas
- Se conservan el HDR del iPhone, el audio, los fps, la fecha y la ubicación
- Un vídeo solo se sustituye si: la duración coincide, hay audio, el HDR es el mismo, ahorra al menos un 20 % y la calidad (VMAF mediana ≥ 95) pasa
- La versión nueva se sube a la misma carpeta (`nombre.mp4`) y el original se **borra definitivamente**

## Uso

```bash
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=...
python3 optimizer.py                 # simulación: no sube ni borra nada
python3 optimizer.py --apply --limit 3   # sustituye los 3 vídeos más grandes
```

## Ejecución automática (GitHub Actions)

`.github/workflows/optimizar.yml` se ejecuta cada noche a las 02:00 UTC.

- Secrets del repositorio: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- Variable `MODO_NOCTURNO`: si vale `sustituir`, la ejecución nocturna sustituye de verdad. Sin ella, solo simula.
- También se puede lanzar a mano desde la pestaña **Actions → Optimizar vídeos de Drive → Run workflow**.
- El resumen de cada ejecución aparece en la propia página de la ejecución.
