#!/usr/bin/env python3
"""
app.py - Sistema web PDF/Word -> voz (lector + conversor MP3)

Funciones:
  - Cargar un PDF o documento Word (.docx)
  - Extraer texto (pypdf/python-docx, con OCR opcional en PDFs)
  - "Leer": sintetiza y reproduce en el navegador
  - "Descargar MP3": genera el MP3 del documento completo

Motores de voz:
  - edge-tts (por defecto): rápido, sin modelos locales, voces online de Microsoft.
  - xtts: clona el estilo de voz de voz.wav (requiere coqui-tts + ~1.8 GB).

Uso:
    pip install -r requirements.txt
    python app.py
    Abrir http://127.0.0.1:5000
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import pdf_a_mp3

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "salidas"
UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB

# Progreso en vivo de las conversiones (consultado por la interfaz)
PROGRESOS: dict[str, dict] = {}

# Voces sugeridas de edge-tts para español
VOCES_EDGE = [
    "es-ES-ElviraNeural",
    "es-ES-AlvaroNeural",
    "es-MX-DaliaNeural",
    "es-MX-JorgeNeural",
    "es-AR-ElenaNeural",
    "es-CO-GonzaloNeural",
]

# ---------------------------------------------------------------------------
# Motores de síntesis
# ---------------------------------------------------------------------------


async def _sintetizar_edge_async(
    texto: str,
    voice: str,
    destino: Path,
    rate: str = "-8%",
    pitch: str = "+0Hz",
    style: str = "narration-professional",
) -> None:
    """Corrutina de edge-tts. El texto puede ser tan largo como sea necesario:
    la librería lo divide en bloques de 4096 bytes dentro de una sola conexión."""
    import edge_tts

    estilos = [style, "general"] if style and style != "general" else ["general"]
    ultimo_error = None
    for estilo in estilos:
        kwargs = {"rate": rate, "pitch": pitch}
        if estilo and estilo != "general":
            kwargs["style"] = estilo
        try:
            communicate = edge_tts.Communicate(texto, voice, **kwargs)
            await communicate.save(str(destino))
            return
        except Exception as exc:  # noqa: BLE001
            ultimo_error = exc
    raise ultimo_error


def sintetizar_edge(
    texto: str,
    voice: str,
    destino: Path,
    rate: str = "-8%",
    pitch: str = "+0Hz",
    style: str = "narration-professional",
) -> None:
    """Sintetiza con edge-tts con entonación más natural (requiere internet)."""
    asyncio.run(_sintetizar_edge_async(texto, voice, destino, rate, pitch, style))


async def sintetizar_edge_lote(
    bloques: list[str],
    voice: str,
    destinos: list[Path],
    rate: str,
    pitch: str,
    style: str,
    concurrencia: int = 3,
    al_completar=None,
) -> None:
    """Sintetiza varios bloques grandes en paralelo dentro del mismo bucle."""
    semaforo = asyncio.Semaphore(concurrencia)
    completados = 0

    async def tarea(bloque: str, destino: Path) -> None:
        nonlocal completados
        async with semaforo:
            await _sintetizar_edge_async(bloque, voice, destino, rate, pitch, style)
            completados += 1
            if al_completar:
                al_completar(completados, len(bloques))

    await asyncio.gather(*(tarea(b, d) for b, d in zip(bloques, destinos)))


def dividir_en_bloques(texto: str, max_bytes: int = 12000) -> list[str]:
    """Divide en bloques grandes respetando palabras (para paralelizar)."""
    bloques = []
    actual = ""
    for palabra in texto.split():
        candidato = f"{actual} {palabra}".strip()
        if len(candidato.encode("utf-8")) > max_bytes and actual:
            bloques.append(actual)
            actual = palabra
        else:
            actual = candidato
    if actual:
        bloques.append(actual)
    return bloques


def sintetizar_xtts(texto: str, destino: Path, voz: Path, idioma: str) -> None:
    """Sintetiza con XTTS-v2 usando voz.wav como referencia."""
    tts = pdf_a_mp3.cargar_modelo()
    tts.tts_to_file(
        text=texto,
        speaker_wav=str(voz),
        language=idioma,
        file_path=str(destino),
    )


def sintetizar(
    texto: str,
    motor: str,
    voice: str,
    destino: Path,
    rate: str = "-8%",
    pitch: str = "+0Hz",
    style: str = "narration-professional",
) -> None:
    if motor == "xtts":
        sintetizar_xtts(texto, destino, OUTPUTS / "voz.wav", "es")
    else:
        sintetizar_edge(texto, voice, destino, rate, pitch, style)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def guardar_y_extraer(archivo) -> tuple[str, str]:
    """Guarda el PDF o Word subido y devuelve (id, texto)."""
    extension = Path(archivo.filename or "").suffix.lower()
    if extension not in {".pdf", ".docx"}:
        raise ValueError("Solo se aceptan archivos .pdf o .docx")

    uid = uuid.uuid4().hex
    ruta = UPLOADS / f"{uid}{extension}"
    archivo.save(str(ruta))
    (UPLOADS / f"{uid}.nombre").write_text(
        Path(archivo.filename).name, encoding="utf-8"
    )
    texto = pdf_a_mp3.extraer_texto(ruta)
    if not texto:
        ruta.unlink(missing_ok=True)
        raise ValueError("No se pudo extraer texto del documento")
    return uid, pdf_a_mp3.humanizar_texto(texto)


def nombre_audio(uid: str) -> str:
    """Nombre del MP3 basado en el documento subido (seguro para Windows)."""
    nombre_path = UPLOADS / f"{uid}.nombre"
    nombre = nombre_path.read_text(encoding="utf-8").strip() if nombre_path.exists() else uid
    nombre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nombre)
    nombre = Path(nombre).stem or uid
    return nombre[:120] + ".mp3"


def a_mp3(wav: Path, mp3: Path, bitrate: str = "192k") -> None:
    pdf_a_mp3.unir_a_mp3([wav], mp3, bitrate)


def xtts_modelo_descargado() -> bool:
    """Comprueba que los archivos pesados del modelo XTTS estén completos."""
    base = (
        Path.home()
        / ".cache" / "huggingface" / "hub"
        / "models--tts-hub--XTTS-v2" / "snapshots"
    )
    if not base.exists():
        return False
    return any(
        (carpeta / "model.pth").exists() and (carpeta / "dvae.pth").exists()
        for carpeta in base.iterdir()
        if carpeta.is_dir()
    )


def texto_recortado(texto: str, limite: int) -> str:
    """Recorta en límite de palabras para evitar cortes bruscos."""
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0]
    return corte + "..."


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html", voces=VOCES_EDGE)


@app.route("/cargar", methods=["POST"])
def cargar():
    """Sube el PDF o Word, extrae texto y lo devuelve para previsualizar."""
    archivo = request.files.get("archivo") or request.files.get("pdf")
    if archivo is None:
        return jsonify(error="Falta el archivo (PDF o Word)"), 400
    if not archivo.filename:
        return jsonify(error="No seleccionaste ningún archivo"), 400
    try:
        uid, texto = guardar_y_extraer(archivo)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Error al procesar el documento: {exc}"), 500

    (UPLOADS / f"{uid}.txt").write_text(texto, encoding="utf-8")
    return jsonify(
        id=uid,
        nombre=archivo.filename,
        caracteres=len(texto),
        texto=texto,
        vista_previa=texto_recortado(texto, 600),
    )


@app.route("/leer", methods=["POST"])
def leer():
    """Sintetiza un fragmento para reproducir en el navegador."""
    datos = request.get_json(force=True)
    uid = datos.get("id", "")
    motor = datos.get("motor", "edge")
    voice = datos.get("voice", VOCES_EDGE[0])
    limite = int(datos.get("limite", 1500))
    rate = datos.get("rate", "-8%")
    pitch = datos.get("pitch", "+0Hz")
    style = datos.get("style", "narration-professional")

    txt_path = UPLOADS / f"{uid}.txt"
    if not txt_path.exists():
        return jsonify(error="Primero carga un PDF o Word"), 400

    texto = pdf_a_mp3.humanizar_texto(
        texto_recortado(txt_path.read_text(encoding="utf-8"), limite)
    )
    wav = OUTPUTS / f"leer_{uid}.wav"
    mp3 = OUTPUTS / f"leer_{uid}.mp3"
    marca = OUTPUTS / f"leer_{uid}.json"
    clave = hashlib.md5(
        f"{uid}|{limite}|{motor}|{voice}|{rate}|{pitch}|{style}|{texto}".encode()
    ).hexdigest()
    if mp3.exists() and marca.exists() and marca.read_text(encoding="utf-8") == clave:
        return send_file(str(mp3), mimetype="audio/mpeg", as_attachment=False)
    try:
        sintetizar(texto, motor, voice, wav, rate, pitch, style)
        a_mp3(wav, mp3)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Error al sintetizar: {exc}"), 500
    finally:
        wav.unlink(missing_ok=True)

    marca.write_text(clave, encoding="utf-8")
    return send_file(str(mp3), mimetype="audio/mpeg", as_attachment=False)


@app.route("/convertir", methods=["POST"])
def convertir():
    """Convierte el PDF completo a MP3 y lo devuelve como descarga."""
    datos = request.get_json(force=True)
    uid = datos.get("id", "")
    motor = datos.get("motor", "edge")
    voice = datos.get("voice", VOCES_EDGE[0])
    max_chars = int(datos.get("max_chars", 220))
    if motor == "xtts" and max_chars < 400:
        # XTTS es secuencial: fragmentos más grandes = menos llamadas al modelo.
        max_chars = 400
    rate = datos.get("rate", "-8%")
    pitch = datos.get("pitch", "+0Hz")
    style = datos.get("style", "narration-professional")

    txt_path = UPLOADS / f"{uid}.txt"
    if not txt_path.exists():
        return jsonify(error="Primero carga un PDF o Word"), 400

    texto = pdf_a_mp3.humanizar_texto(txt_path.read_text(encoding="utf-8"))
    fragmentos = pdf_a_mp3.dividir_en_fragmentos(texto, max_chars)
    if not fragmentos:
        return jsonify(error="El PDF no contiene texto legible"), 400

    mp3 = OUTPUTS / f"{uid}.mp3"
    marca = OUTPUTS / f"{uid}.json"
    clave = hashlib.md5(
        f"{len(texto)}|{motor}|{voice}|{rate}|{pitch}|{style}|{max_chars}".encode()
    ).hexdigest()

    # Caché: si ya se generó con los mismos parámetros, devolver al instante.
    if mp3.exists() and marca.exists() and marca.read_text(encoding="utf-8") == clave:
        PROGRESOS[uid] = {"estado": "Listo (desde caché)", "porcentaje": 100, "listo": True}
        respuesta = send_file(str(mp3), mimetype="audio/mpeg", as_attachment=True,
                              download_name=nombre_audio(uid))
        respuesta.headers["X-Tiempo"] = "0.0"
        respuesta.headers["X-Cache"] = "hit"
        respuesta.headers["X-Nombre"] = nombre_audio(uid)
        return respuesta

    inicio = time.perf_counter()
    PROGRESOS[uid] = {"estado": "Preparando...", "porcentaje": 0, "listo": False}
    try:
        if motor == "xtts":
            # XTTS es local y secuencial: mantiene fragmentos pequeños.
            total = len(fragmentos)
            wavs: list[Path] = []
            for i, frag in enumerate(fragmentos):
                PROGRESOS[uid] = {
                    "estado": f"XTTS: fragmento {i+1} de {total}",
                    "porcentaje": int(100 * i / total),
                    "listo": False,
                }
                with tempfile.TemporaryDirectory() as tmp:
                    wav = Path(tmp) / "f.wav"
                    sintetizar(frag, motor, voice, wav, rate, pitch, style)
                    if i == 0:
                        # El primer WAV debe conservarse fuera del temporal
                        conservado = OUTPUTS / f"_xtts_{uid}.wav"
                        wav.replace(conservado)
                        wavs.append(conservado)
                    else:
                        conservado = OUTPUTS / f"_xtts_{uid}_{i}.wav"
                        wav.replace(conservado)
                        wavs.append(conservado)
            pdf_a_mp3.unir_a_mp3(wavs, mp3)
            for w in wavs:
                w.unlink(missing_ok=True)
        else:
            # edge-tts: un solo bloque si cabe; si el documento es enorme,
            # varios bloques grandes en paralelo y concatenación sin re-codificar.
            tamano = len(texto.encode("utf-8"))
            if tamano <= 12000:
                PROGRESOS[uid] = {"estado": "Generando audio...", "porcentaje": 10, "listo": False}
                sintetizar_edge(texto, voice, mp3, rate, pitch, style)
            else:
                bloques = dividir_en_bloques(texto)
                total = len(bloques)
                PROGRESOS[uid] = {
                    "estado": f"Generando {total} bloques (3 a la vez)...",
                    "porcentaje": 0,
                    "listo": False,
                }

                def progreso(n: int, total_bloques: int) -> None:
                    PROGRESOS[uid] = {
                        "estado": f"Generando bloque {n} de {total_bloques}",
                        "porcentaje": int(90 * n / total_bloques),
                        "listo": False,
                    }

                with tempfile.TemporaryDirectory() as tmp:
                    destinos = [Path(tmp) / f"b_{i:03d}.mp3" for i in range(total)]
                    asyncio.run(
                        sintetizar_edge_lote(
                            bloques, voice, destinos, rate, pitch, style,
                            al_completar=progreso,
                        )
                    )
                    PROGRESOS[uid] = {"estado": "Uniendo audio...", "porcentaje": 95, "listo": False}
                    pdf_a_mp3.unir_mp3(destinos, mp3)
    except Exception as exc:  # noqa: BLE001
        PROGRESOS[uid] = {"estado": f"Error: {exc}", "porcentaje": 0, "listo": False, "error": str(exc)}
        return jsonify(error=f"Error al convertir: {exc}"), 500

    transcurrido = time.perf_counter() - inicio
    marca.write_text(clave, encoding="utf-8")
    PROGRESOS[uid] = {"estado": "Listo", "porcentaje": 100, "listo": True}
    print(f"Conversion completada en {transcurrido:.1f}s ({len(texto)} caracteres)")
    respuesta = send_file(
        str(mp3), mimetype="audio/mpeg", as_attachment=True, download_name=nombre_audio(uid)
    )
    respuesta.headers["X-Tiempo"] = f"{transcurrido:.1f}"
    respuesta.headers["X-Cache"] = "miss"
    respuesta.headers["X-Nombre"] = nombre_audio(uid)
    return respuesta


@app.route("/descargar/<uid>")
def descargar(uid: str):
    """Descarga el MP3 ya generado con el nombre del documento original."""
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        return jsonify(error="Identificador de documento no válido"), 400
    mp3 = OUTPUTS / f"{uid}.mp3"
    if not mp3.exists():
        return jsonify(error="Todavía no hay MP3 para este documento"), 404
    return send_file(
        str(mp3), mimetype="audio/mpeg", as_attachment=True, download_name=nombre_audio(uid)
    )


@app.route("/progreso")
def progreso():
    """Devuelve el estado de la conversión en curso."""
    uid = request.args.get("id", "")
    return jsonify(PROGRESOS.get(uid, {"estado": "", "porcentaje": 0, "listo": False}))


@app.route("/estado")
def estado():
    def disponible(modulo: str) -> bool:
        # find_spec evita importar módulos pesados (TTS, torch) solo para consultar.
        from importlib.util import find_spec

        try:
            return find_spec(modulo) is not None
        except Exception:  # noqa: BLE001
            return False

    modelo_xtts = xtts_modelo_descargado()
    return jsonify(
        edge_tts=disponible("edge_tts"),
        xtts=disponible("TTS") and modelo_xtts,
        xtts_modelo=modelo_xtts,
        pypdf=disponible("pypdf"),
        python_docx=disponible("docx"),
        voz_referencia=(OUTPUTS / "voz.wav").exists(),
    )


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    print(f"Abrir http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
