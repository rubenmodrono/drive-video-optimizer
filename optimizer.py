#!/usr/bin/env python3
"""Drive Video Optimizer.

Busca vídeos en Google Drive, los recomprime a 1080p HEVC (~10 Mbps, conservando
HDR, audio, fps y metadatos), comprueba el resultado y, solo si pasa todas las
comprobaciones, sube la versión nueva a la misma carpeta y borra el original
de forma definitiva.

Por defecto funciona en modo simulación (--dry-run): descarga, comprime y
verifica, pero no sube ni borra nada. Usa --apply para sustituir de verdad.

Configuración por variables de entorno:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
MARK = "dvo_optimized"          # appProperty puesta en los archivos ya optimizados

# Ajustes de compresión
# Escalera de bitrates (Mbps): se empieza por el más bajo y, si la calidad no
# llega al mínimo, se vuelve a comprimir con el siguiente.
BITRATES = [10, 14, 18]
MAX_SHORT_SIDE = 1080
# Reglas de verificación
MIN_SAVINGS = 0.20          # ahorro mínimo para sustituir
MAX_DURATION_DIFF = 0.15    # segundos
MIN_VMAF = 95.0
MIN_SSIM = 0.97             # solo si el ffmpeg no trae libvmaf
SKIP_BELOW_MBPS = 15.0      # no tocar vídeos <=1080p que ya pesan poco


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Drive API
class Drive:
    def __init__(self):
        self.cid = os.environ["GOOGLE_CLIENT_ID"]
        self.secret = os.environ["GOOGLE_CLIENT_SECRET"]
        self.refresh = os.environ["GOOGLE_REFRESH_TOKEN"]
        self.token, self.exp = None, 0

    def _auth(self):
        if time.time() > self.exp - 60:
            data = urllib.parse.urlencode(dict(
                client_id=self.cid, client_secret=self.secret,
                refresh_token=self.refresh, grant_type="refresh_token")).encode()
            r = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
            self.token, self.exp = r["access_token"], time.time() + r["expires_in"]
            self.scope = r.get("scope", "")
        return {"Authorization": "Bearer " + self.token}

    def req(self, method, url, body=None, headers=None, raw=False):
        h = dict(self._auth(), **(headers or {}))
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
        for attempt in range(5):
            try:
                resp = urllib.request.urlopen(urllib.request.Request(url, body, h, method=method))
                return resp if raw else (json.loads(resp.read() or b"{}"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"{method} {url} -> {e.code}: {e.read()[:300]!r}")

    def can_write(self):
        self._auth()
        return "auth/drive " in self.scope + " " or self.scope.endswith("auth/drive")

    def quota(self):
        q = self.req("GET", API + "/about?fields=storageQuota")["storageQuota"]
        return int(q.get("limit", 0)), int(q.get("usage", 0))

    def list_videos(self):
        q = "mimeType contains 'video/' and trashed = false and 'me' in owners"
        fields = ("nextPageToken,files(id,name,size,mimeType,parents,createdTime,"
                  "modifiedTime,description,appProperties,videoMediaMetadata)")
        out, tok = [], None
        while True:
            p = dict(q=q, fields=fields, pageSize=200, spaces="drive")
            if tok:
                p["pageToken"] = tok
            r = self.req("GET", API + "/files?" + urllib.parse.urlencode(p))
            out += r.get("files", [])
            tok = r.get("nextPageToken")
            if not tok:
                return out

    def download(self, fid, path):
        with self.req("GET", f"{API}/files/{fid}?alt=media", raw=True) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f, 8 << 20)

    def upload(self, path, meta):
        size = os.path.getsize(path)
        r = self.req("POST", UPLOAD + "?uploadType=resumable&fields=id,size,md5Checksum",
                     meta, {"X-Upload-Content-Type": "video/mp4",
                            "X-Upload-Content-Length": str(size)}, raw=True)
        loc = r.headers["Location"]
        with open(path, "rb") as f:
            h = dict(self._auth(), **{"Content-Type": "video/mp4", "Content-Length": str(size)})
            resp = urllib.request.urlopen(urllib.request.Request(loc, f, h, method="PUT"))
            return json.load(resp)

    def delete_forever(self, fid):
        self.req("DELETE", f"{API}/files/{fid}")


# ---------------------------------------------------------------- ffmpeg
def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(" ".join(cmd[:3]) + " falló: " + p.stderr[-800:])
    return p.stdout + p.stderr


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path])
    return json.loads(out[out.index("{"):])


def video_info(info):
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", 0)) or rot
    if abs(rot) == 90:
        w, h = h, w
    return dict(w=w, h=h, codec=v["codec_name"],
                hdr=v.get("color_transfer") in ("arib-std-b67", "smpte2084"),
                trc=v.get("color_transfer"), prim=v.get("color_primaries"),
                matrix=v.get("color_space"), pix=v.get("pix_fmt", ""),
                fps=v.get("avg_frame_rate", "30/1"),
                duration=float(info["format"]["duration"]),
                bitrate=float(info["format"].get("bit_rate", 0)),
                audio=sum(s["codec_type"] == "audio" for s in info["streams"]))


def has_vmaf():
    return "libvmaf" in run(["ffmpeg", "-hide_banner", "-filters"])


def encode(src, dst, vi, workdir, mbps):
    short = min(vi["w"], vi["h"])
    scale = (f"scale={'-2' if vi['w'] > vi['h'] else MAX_SHORT_SIDE}:"
             f"{MAX_SHORT_SIDE if vi['w'] > vi['h'] else '-2'}:flags=lanczos"
             if short > MAX_SHORT_SIDE else "null")
    ten_bit = vi["hdr"] or "10" in vi["pix"]
    pix = "yuv420p10le" if ten_bit else "yuv420p"
    try:
        num, den = vi["fps"].split("/")
        fps = max(1, round(int(num) / int(den)))
    except Exception:
        fps = 30
    xp = f"log-level=error:fps={fps}"
    color = []
    if vi["hdr"]:
        xp += f":colorprim=bt2020:transfer={vi['trc']}:colormatrix=bt2020nc:range=limited"
        color = ["-color_primaries", "bt2020", "-color_trc", vi["trc"], "-colorspace", "bt2020nc"]
    stats = os.path.join(workdir, "x265.log")
    common = ["-map", "0:v:0", "-fps_mode", "passthrough", "-vf", f"{scale},format={pix}",
              "-c:v", "libx265", "-preset", "medium", "-b:v", f"{mbps}M",
              "-maxrate", f"{mbps * 1.5:g}M", "-bufsize", f"{mbps * 2}M", "-tag:v", "hvc1"]
    run(["ffmpeg", "-hide_banner", "-y", "-i", src, *common,
         "-x265-params", f"{xp}:pass=1:stats={stats}", "-an", "-f", "null", "-"])
    run(["ffmpeg", "-hide_banner", "-y", "-i", src, *common,
         "-x265-params", f"{xp}:pass=2:stats={stats}",
         "-map", "0:a?", "-c:a", "copy", *color,
         "-map_metadata", "0", "-metadata", "major_brand=", "-metadata", "minor_version=",
         "-metadata", "compatible_brands=",
         "-movflags", "+faststart+use_metadata_tags", dst])


def frame_score(src, dst, t, w, h, vmaf):
    """Compara un fotograma del original (reescalado) con el del comprimido."""
    pix = "yuv420p10le"
    a = ["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
         "-vf", f"scale={w}:{h}:flags=lanczos,format={pix}", "-f", "rawvideo", "-"]
    b = ["-ss", f"{t:.3f}", "-i", dst, "-frames:v", "1",
         "-vf", f"format={pix}", "-f", "rawvideo", "-"]
    ra = subprocess.run(["ffmpeg", "-v", "error", *a], capture_output=True).stdout
    rb = subprocess.run(["ffmpeg", "-v", "error", *b], capture_output=True).stdout
    if not ra or len(ra) != len(rb):
        return None
    with tempfile.TemporaryDirectory() as d:
        pa, pb = os.path.join(d, "a.yuv"), os.path.join(d, "b.yuv")
        open(pa, "wb").write(ra)
        open(pb, "wb").write(rb)
        raw = ["-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}"]
        flt = "libvmaf" if vmaf else "ssim"
        out = run(["ffmpeg", "-hide_banner", *raw, "-i", pb, *raw, "-i", pa,
                   "-lavfi", flt, "-f", "null", "-"])
    if vmaf:
        return float(out.rsplit("VMAF score:", 1)[1].split()[0])
    return float(out.rsplit("All:", 1)[1].split()[0])


def verify(src, dst, vi_src, orig_size):
    vi = video_info(probe(dst))
    size = os.path.getsize(dst)
    problems = []
    if abs(vi["duration"] - vi_src["duration"]) > MAX_DURATION_DIFF:
        problems.append(f"duración distinta ({vi['duration']:.2f}s vs {vi_src['duration']:.2f}s)")
    if vi["audio"] < min(1, vi_src["audio"]):
        problems.append("falta el audio")
    if vi["hdr"] != vi_src["hdr"]:
        problems.append("ha cambiado el HDR")
    savings = 1 - size / orig_size
    if savings < MIN_SAVINGS:
        problems.append(f"ahorro insuficiente ({savings:.0%})")
    vmaf = has_vmaf()
    d = vi_src["duration"]
    times = [d * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
    scores = [s for s in (frame_score(src, dst, t, vi["w"], vi["h"], vmaf) for t in times)
              if s is not None]
    metric = "VMAF" if vmaf else "SSIM"
    limit = MIN_VMAF if vmaf else MIN_SSIM
    if len(scores) < 3:
        problems.append("no se pudieron comparar fotogramas")
    elif sorted(scores)[len(scores) // 2] < limit:  # mediana: tolera un fotograma mal emparejado
        problems.append(f"{metric} mediana {sorted(scores)[len(scores) // 2]:.3f} < {limit}")
    return dict(ok=not problems, problems=problems, size=size, savings=savings,
                metric=metric, scores=[round(s, 3) for s in scores],
                mbps=round(size * 8 / max(vi["duration"], 0.1) / 1e6, 1))


# ---------------------------------------------------------------- main loop
def process(drive, f, args, workdir):
    name, size = f["name"], int(f.get("size", 0))
    src = os.path.join(workdir, "src")
    dst = os.path.join(workdir, "out.mp4")
    log(f"↓ {name} ({size/1e6:.0f} MB)")
    drive.download(f["id"], src)
    vi = video_info(probe(src))
    if min(vi["w"], vi["h"]) <= MAX_SHORT_SIDE and vi["bitrate"] / 1e6 <= SKIP_BELOW_MBPS:
        return dict(name=name, status="omitido", reason="ya es ligero")
    t0 = time.time()
    tried = []
    for mbps in BITRATES:
        encode(src, dst, vi, workdir, mbps)
        res = verify(src, dst, vi, size)
        tried.append(f"{mbps}M: {res['metric']} {res['scores']}")
        quality_only = all(p.startswith(res["metric"]) for p in res["problems"])
        if res["ok"] or not quality_only:
            break  # aprobado, o falla por algo que más bitrate no arregla
    res.update(name=name, original=size, seconds=round(time.time() - t0), intentos=tried)
    if not res["ok"]:
        res["status"] = "rechazado"
        return res
    if not args.apply:
        res["status"] = "simulado"
        if args.keep:
            os.makedirs(args.keep, exist_ok=True)
            shutil.copy(dst, os.path.join(args.keep, os.path.splitext(name)[0] + ".mp4"))
        return res
    limit, usage = drive.quota()
    if limit and limit - usage < res["size"] + (50 << 20):
        res["status"] = "sin espacio"
        return res
    meta = dict(name=os.path.splitext(name)[0] + ".mp4", parents=f.get("parents"),
                createdTime=f.get("createdTime"), modifiedTime=f.get("modifiedTime"),
                description=f.get("description"), mimeType="video/mp4",
                appProperties={MARK: "1", "dvo_source_id": f["id"],
                               "dvo_source_size": str(size)})
    meta = {k: v for k, v in meta.items() if v}
    up = drive.upload(dst, meta)
    if int(up.get("size", -1)) != res["size"]:
        res["status"] = "error de subida"
        return res
    drive.delete_forever(f["id"])
    log(f"✓ {name}: subido {up['id']} y original borrado")
    res["status"] = "sustituido"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="sustituir de verdad (si no, simulación)")
    ap.add_argument("--limit", type=int, default=0, help="máximo de vídeos a procesar")
    ap.add_argument("--only", nargs="*", help="procesar solo estos nombres de archivo")
    ap.add_argument("--keep", help="(simulación) carpeta donde guardar los comprimidos")
    ap.add_argument("--report", default="informe.json")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="no empezar vídeos nuevos pasado este tiempo")
    ap.add_argument("--summary", help="archivo Markdown donde escribir el resumen")
    ap.add_argument("--id", help="procesar solo el archivo de Drive con este id")
    ap.add_argument("--list-json", help="no procesar: escribir en este archivo la lista "
                    "de vídeos pendientes como JSON (para lanzar un trabajo por vídeo)")
    args = ap.parse_args()

    drive = Drive()
    if args.apply and not drive.can_write():
        sys.exit("El token solo tiene permiso de lectura: no se puede usar --apply.")
    todos = drive.list_videos()
    hechos = {os.path.splitext(f["name"])[0].lower() for f in todos
              if (f.get("appProperties") or {}).get(MARK) == "1"}
    videos = [f for f in todos if (f.get("appProperties") or {}).get(MARK) != "1"]
    # Control de duplicados: un original que se vuelve a subir después de
    # haberlo optimizado (mismo nombre y mismo tamaño que el original que se
    # sustituyó) se borra en modo sustituir, y nunca se vuelve a procesar.
    origen = {}
    for f in todos:
        ap_ = f.get("appProperties") or {}
        if ap_.get(MARK) == "1":
            origen.setdefault(os.path.splitext(f["name"])[0].lower(), set()).add(
                ap_.get("dvo_source_size"))
    sobran, vistos, unicos = [], {}, []
    for f in sorted(videos, key=lambda f: f.get("createdTime", "")):
        base = os.path.splitext(f["name"])[0].lower()
        clave = (f["name"].lower(), f.get("size"))
        if base in hechos:
            if f.get("size") in origen.get(base, ()):
                sobran.append(f)  # es el mismo original ya optimizado
            else:
                log(f"{f['name']}: ya hay un {base}.mp4 optimizado pero el tamaño no coincide; se deja como está")
        elif clave in vistos:
            sobran.append(f)      # dos subidas idénticas del mismo archivo
        else:
            vistos[clave] = f
            unicos.append(f)
    videos = unicos
    for f in sobran if not args.id else []:  # solo lo hace el paso que lista
        if args.apply:
            drive.delete_forever(f["id"])
            log(f"Duplicado borrado: {f['name']} ({int(f.get('size', 0))/1e6:.0f} MB)")
        else:
            log(f"Duplicado (se borraría en modo sustituir): {f['name']}")
    if args.only:
        videos = [f for f in videos if f["name"] in args.only]
    if args.id:
        videos = [f for f in videos if f["id"] == args.id]
    videos.sort(key=lambda f: -int(f.get("size", 0)))  # los más grandes primero
    if args.limit:
        videos = videos[:args.limit]
    if args.list_json:
        with open(args.list_json, "w") as out:
            json.dump([dict(id=f["id"], name=f["name"]) for f in videos], out, ensure_ascii=False)
        log(f"{len(videos)} vídeos pendientes.")
        return
    log(f"{len(videos)} vídeos pendientes. Modo: {'SUSTITUIR' if args.apply else 'simulación'}")

    results, start = [], time.time()
    for f in videos:
        if args.max_minutes and time.time() - start > args.max_minutes * 60:
            log("Tiempo agotado; el resto queda para la próxima ejecución.")
            break
        with tempfile.TemporaryDirectory() as wd:
            try:
                r = process(drive, f, args, wd)
            except Exception as e:  # un vídeo con problemas no para el resto
                r = dict(name=f["name"], status="error", reason=str(e)[:300])
        log(json.dumps(r, ensure_ascii=False))
        results.append(r)
        json.dump(results, open(args.report, "w"), ensure_ascii=False, indent=1)

    freed = sum(r["original"] - r["size"] for r in results
                if r.get("status") in ("sustituido", "simulado"))
    msg = f"Espacio {'liberado' if args.apply else 'que se liberaría'}: {freed/1e9:.2f} GB"
    log("Terminado. " + msg)
    if args.summary:
        with open(args.summary, "a") as s:
            s.write(f"## {', '.join(r['name'] for r in results) or 'Drive Video Optimizer'}\n\n{msg}\n\n| Vídeo | Estado | Antes | Después | Calidad |\n|---|---|---|---|---|\n")
            for r in results:
                s.write(f"| {r['name']} | {r.get('status')} {'; '.join(r.get('problems', [])) or r.get('reason', '')} | "
                        f"{r.get('original', 0)/1e6:.0f} MB | {r.get('size', 0)/1e6:.0f} MB | "
                        f"{r.get('metric', '')} {r.get('scores', '')} |\n")


if __name__ == "__main__":
    main()
