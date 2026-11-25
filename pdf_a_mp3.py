#!/usr/bin/env python3
"""
pdf_a_mp3.py

Convierte un PDF o un documento Word (.docx) a MP3 usando voz.wav como referencia.

La referencia (speaker_wav) no clona la voz literalmente con solo 6 segundos,
pero sí condiciona el timbre, el tono y el estilo de la síntesis de XTTS-v2.

Uso:
    python pdf_a_mp3.py archivo.pdf -o salida.mp3
    python pdf_a_mp3.py archivo.docx -o salida.mp3 --voz voz.wav --idioma es
    python pdf_a_mp3.py archivo.pdf --ocr   # si el PDF es escaneado

Requisitos:
    pip install -r requirements.txt
   ffmpeg debe estar instalado y en PATH (para exportar MP3).
"""

from __future__ import annotations

import argparse
import functools
import re
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Extracción de texto del PDF
# ---------------------------------------------------------------------------


def extraer_texto_pdf(ruta_pdf: Path) -> str:
    """Extrae texto con pypdf; si no hay texto, usa OCR con pytesseract."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Instala pypdf: pip install pypdf") from exc

    reader = PdfReader(str(ruta_pdf))
    paginas = []
    for pagina in reader.pages:
        paginas.append(pagina.extract_text() or "")

    texto = "\n".join(paginas).strip()
    if texto:
        return texto

    # PDF escaneado -> OCR
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "El PDF no tiene texto. Para OCR instala: pip install pymupdf pytesseract pillow"
        ) from exc

    imagenes = []
    with fitz.open(str(ruta_pdf)) as doc:
        for pagina in doc:
            pix = pagina.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            imagenes.append(pytesseract.image_to_string(img, lang="spa"))

    return "\n".join(imagenes).strip()


def extraer_texto_docx(ruta_docx: Path) -> str:
    """Extrae texto de un documento Word .docx (párrafos y tablas)."""
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Instala python-docx: pip install python-docx") from exc

    documento = Document(str(ruta_docx))
    partes = [p.text.strip() for p in documento.paragraphs if p.text.strip()]

    for tabla in documento.tables:
        for fila in tabla.rows:
            celdas = [celda.text.strip() for celda in fila.cells if celda.text.strip()]
            if celdas:
                partes.append(" | ".join(celdas))

    return "\n".join(partes).strip()


def extraer_texto(ruta: Path) -> str:
    """Extrae texto según la extensión (.pdf, .docx)."""
    extension = ruta.suffix.lower()
    if extension == ".pdf":
        return extraer_texto_pdf(ruta)
    if extension == ".docx":
        return extraer_texto_docx(ruta)
    raise ValueError(f"Formato no soportado: {extension} (usa .pdf o .docx)")


# ---------------------------------------------------------------------------
# 2. Limpieza y segmentación del texto
# ---------------------------------------------------------------------------


def limpiar_texto(texto: str) -> str:
    texto = texto.replace("\r", "")
    texto = re.sub(r"-\n(?=[a-záéíóúñ])", "", texto)  # guion de fin de línea
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    texto = re.sub(r"[^\S\n]+([.,;:!?])", r"\1", texto)
    return texto.strip()


def humanizar_texto(texto: str) -> str:
    """Prepara el texto para que la voz suene menos robótica.

    edge-tts pronuncia las abreviaturas y los conectores de forma literal
    ("Sr." -> "sr punto"), lo que rompe la entonación. Aquí se expanden y
    se añaden pausas naturales antes de los conectores al inicio de frase.
    """
    texto = limpiar_texto(texto)

    abreviaturas = {
        r"\bSr\.\s*": "Señor ",
        r"\bSra\.\s*": "Señora ",
        r"\bSres\.\s*": "Señores ",
        r"\bDr\.\s*": "Doctor ",
        r"\bDra\.\s*": "Doctora ",
        r"\bProf\.\s*": "Profesor ",
        r"\bAv\.\s*": "Avenida ",
        r"\bUd\.\s*": "usted ",
        r"\betc\.\s*": "etcétera ",
        r"\bN\.?º\b": "número",
    }
    for patron, reemplazo in abreviaturas.items():
        texto = re.sub(patron, reemplazo, texto, flags=re.IGNORECASE)

    texto = texto.replace(" & ", " y ")
    texto = re.sub(r"(?<=\d)\s*%", " por ciento", texto)

    conectores = (
        "Sin embargo", "No obstante", "Por tanto", "Por lo tanto",
        "Asimismo", "Además", "En cambio", "Por ejemplo",
        "En primer lugar", "Por último", "Mientras tanto",
    )
    for conector in conectores:
        patron = rf"(^|[.!?]\s+|\n)({conector})\b(?!,)"
        texto = re.sub(patron, r"\1\2,", texto)

    return texto.strip()


def dividir_en_fragmentos(texto: str, max_chars: int = 220) -> list[str]:
    """Divide por párrafos y oraciones, respetando max_chars."""
    parrafos = [p.strip() for p in texto.split("\n\n") if p.strip()]
    fragmentos: list[str] = []

    for parrafo in parrafos:
        oraciones = re.split(r"(?<=[.!?])\s+", parrafo)
        actual = ""
        for oracion in oraciones:
            oracion = oracion.strip()
            if not oracion:
                continue
            if len(oracion) > max_chars:
                if actual:
                    fragmentos.append(actual)
                    actual = ""
                palabras = oracion.split()
                tmp = ""
                for palabra in palabras:
                    candidato = f"{tmp} {palabra}".strip()
                    if len(candidato) > max_chars and tmp:
                        fragmentos.append(tmp)
                        tmp = palabra
                    else:
                        tmp = candidato
                if tmp:
                    actual = tmp
                continue
            candidato = f"{actual} {oracion}".strip() if actual else oracion
            if len(candidato) <= max_chars:
                actual = candidato
            else:
                fragmentos.append(actual)
                actual = oracion
        if actual:
            fragmentos.append(actual)

    return fragmentos


# ---------------------------------------------------------------------------
# 3. Síntesis con XTTS-v2 usando voz.wav como referencia
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def cargar_modelo():
    """Carga XTTS-v2 una sola vez y reutiliza la instancia en todo el proceso."""
    try:
        from TTS.api import TTS
    except ImportError as exc:
        raise RuntimeError("Instala coqui-tts: pip install coqui-tts") from exc

    print("Cargando modelo XTTS-v2 (puede descargar ~1.8 GB la primera vez)...")
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    if hasattr(tts, "to"):
        # Usa GPU si está disponible
        import torch

        tts.to("cuda" if torch.cuda.is_available() else "cpu")
    return tts


def sintetizar(tts, fragmentos: list[str], voz: Path, idioma: str, carpeta: Path) -> list[Path]:
    archivos = []
    for i, texto in enumerate(fragmentos):
        salida = carpeta / f"frag_{i:04d}.wav"
        print(f"  [{i+1}/{len(fragmentos)}] {texto[:70]}...")
        tts.tts_to_file(
            text=texto,
            speaker_wav=str(voz),
            language=idioma,
            file_path=str(salida),
        )
        archivos.append(salida)
    return archivos


# ---------------------------------------------------------------------------
# 4. Unir y convertir a MP3
# ---------------------------------------------------------------------------


def ruta_ffmpeg() -> str:
    """Devuelve la ruta a un ejecutable ffmpeg usable."""
    from shutil import which

    encontrado = which("ffmpeg")
    if encontrado:
        return encontrado
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError(
            "Instala ffmpeg o imageio-ffmpeg: pip install imageio-ffmpeg"
        ) from exc


def unir_a_mp3(archivos: list[Path], salida: Path, bitrate: str = "192k") -> None:
    """Une WAVs con pausas y exporta MP3 usando ffmpeg directamente."""
    import subprocess
    import wave

    if not archivos:
        raise RuntimeError("No se generaron fragmentos de audio")

    # Concatenamos los WAV crudos (misma calidad: mono 16 bits) en uno temporal
    ffmpeg = ruta_ffmpeg()

    with tempfile.TemporaryDirectory() as tmp:
        temporal = Path(tmp)
        wavs: list[Path] = []
        for i, archivo in enumerate(archivos):
            cabecera = archivo.open("rb").read(12)
            if cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WAVE":
                wavs.append(archivo)
                continue
            convertido = temporal / f"norm_{i:04d}.wav"
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(archivo),
                 "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(convertido)],
                check=True,
            )
            wavs.append(convertido)

        unido = temporal / "unido.wav"
        with wave.open(str(wavs[0]), "rb") as primero:
            canales = primero.getnchannels()
            ancho = primero.getsampwidth()
            tasa = primero.getframerate()

        silencio = b"\x00" * (tasa * ancho * canales // 4)  # ~250 ms

        with wave.open(str(unido), "wb") as out:
            out.setnchannels(canales)
            out.setsampwidth(ancho)
            out.setframerate(tasa)
            for wav in wavs:
                with wave.open(str(wav), "rb") as w:
                    out.writeframes(w.readframes(w.getnframes()))
                out.writeframes(silencio)

        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(unido),
            "-b:a", bitrate,
            str(salida),
        ]
        subprocess.run(cmd, check=True)


def unir_mp3(archivos: list[Path], salida: Path) -> None:
    """Concatena MP3 del mismo códec sin re-codificar (casi instantáneo)."""
    import subprocess

    if not archivos:
        raise RuntimeError("No se generaron fragmentos de audio")
    if len(archivos) == 1:
        archivos[0].replace(salida)
        return

    ffmpeg = ruta_ffmpeg()
    with tempfile.TemporaryDirectory() as tmp:
        lista = Path(tmp) / "lista.txt"
        # ffmpeg espera rutas con '/' y comillas simples escapadas en el formato concat
        lineas = []
        for archivo in archivos:
            ruta = str(archivo.resolve()).replace("\\", "/").replace("'", "'\\''")
            lineas.append(f"file '{ruta}'")
        lista.write_text("\n".join(lineas), encoding="utf-8")

        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(lista), "-c", "copy", str(salida)],
            check=True,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF/Word -> MP3 con voz de referencia")
    parser.add_argument("archivo", type=Path, help="Archivo PDF o .docx de entrada")
    parser.add_argument("-o", "--output", type=Path, default=Path("salida.mp3"))
    parser.add_argument("--voz", type=Path, default=Path("voz.wav"), help="WAV de referencia")
    parser.add_argument("--idioma", default="es", help="Código de idioma (es, en, ...)")
    parser.add_argument("--max-chars", type=int, default=220)
    parser.add_argument("--ocr", action="store_true", help="Forzar OCR (PDF escaneado)")
    parser.add_argument("--bitrate", default="192k")
    args = parser.parse_args()

    if not args.archivo.exists():
        print(f"Error: no existe {args.archivo}", file=sys.stderr)
        return 1
    if not args.voz.exists():
        print(f"Error: no existe {args.voz}", file=sys.stderr)
        return 1

    print(f"Extrayendo texto de {args.archivo.name}...")
    try:
        texto = extraer_texto(args.archivo)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not texto:
        print("Error: no se pudo extraer texto del documento", file=sys.stderr)
        return 1

    texto = limpiar_texto(texto)
    fragmentos = dividir_en_fragmentos(texto, args.max_chars)
    print(f"Texto: {len(texto)} caracteres -> {len(fragmentos)} fragmentos")

    tts = cargar_modelo()

    with tempfile.TemporaryDirectory() as tmp:
        wavs = sintetizar(tts, fragmentos, args.voz, args.idioma, Path(tmp))
        print(f"Uniendo {len(wavs)} fragmentos en MP3...")
        unir_a_mp3(wavs, args.output, args.bitrate)

    print(f"Listo: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
