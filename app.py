import csv
import io
import json
import re
import struct
import unicodedata
from datetime import datetime
from zipfile import ZIP_DEFLATED, ZipFile

import streamlit as st
import streamlit.components.v1 as components

# ---------------------------
# Configuracoes globais
# ---------------------------
MAX_REPETICOES = 50
MAX_TRATAMENTOS = 99
MAX_SUBAMOSTRAS = 30
MAX_TOTAL_FOTOS = 3000


# ---------------------------
# Estado da sessao
# ---------------------------
def init_session_state() -> None:
    defaults = {
        "cfg_ensaio": "",
        "cfg_alvo": "",
        "cfg_repeticoes": 0,
        "cfg_tratamentos": 0,
        "cfg_subamostras": 0,
        "auto_started_signature": "",
        "flow_started": False,
        "setup": {},
        "sequence": [],
        "current_idx": 0,
        "captures": {},
        "current_location": None,
        "retake_counter": 0,
        "zip_cache": None,
        "zip_name": "",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_flow(keep_config: bool = True) -> None:
    st.session_state.flow_started = False
    st.session_state.auto_started_signature = ""
    st.session_state.setup = {}
    st.session_state.sequence = []
    st.session_state.current_idx = 0
    st.session_state.captures = {}
    st.session_state.retake_counter = 0
    st.session_state.zip_cache = None
    st.session_state.zip_name = ""

    if not keep_config:
        st.session_state.cfg_ensaio = ""
        st.session_state.cfg_alvo = ""
        st.session_state.cfg_repeticoes = 0
        st.session_state.cfg_tratamentos = 0
        st.session_state.cfg_subamostras = 0


# ---------------------------
# Regras de negocio
# ---------------------------
def build_sequence(repeticoes: int, tratamentos: int, subamostras: int) -> list[dict]:
    sequence = []
    for repeticao in range(1, repeticoes + 1):
        for tratamento in range(1, tratamentos + 1):
            parcela = repeticao * 100 + tratamento
            for subamostra in range(1, subamostras + 1):
                sequence.append(
                    {
                        "repeticao": repeticao,
                        "tratamento": tratamento,
                        "parcela": parcela,
                        "subamostra": subamostra,
                    }
                )
    return sequence


def validate_setup(
    ensaio: str,
    alvo: str,
    repeticoes: int,
    tratamentos: int,
    subamostras: int,
) -> tuple[list[str], int, int]:
    errors = []

    if not ensaio.strip():
        errors.append("Preencha o campo 'Nome do Ensaio'.")

    if not alvo.strip():
        errors.append("Preencha o campo 'Praga / Alvo Avaliado'.")

    if repeticoes < 1:
        errors.append("Numero de Repeticoes deve ser no minimo 1.")

    if tratamentos < 1:
        errors.append("Numero de Tratamentos deve ser no minimo 1.")

    if tratamentos > MAX_TRATAMENTOS:
        errors.append(f"Numero de Tratamentos nao pode ser maior que {MAX_TRATAMENTOS}.")

    if subamostras < 1:
        errors.append("Numero de Subamostras por parcela deve ser no minimo 1.")

    total_parcelas = max(repeticoes, 0) * max(tratamentos, 0)
    total_fotos = total_parcelas * max(subamostras, 0)

    if total_fotos > MAX_TOTAL_FOTOS:
        errors.append(
            f"Total de fotos ({total_fotos}) excede o limite configurado ({MAX_TOTAL_FOTOS})."
        )

    return errors, total_parcelas, total_fotos


def force_uppercase_field(field_key: str) -> None:
    value = st.session_state.get(field_key, "")
    st.session_state[field_key] = value.upper()


def _first_query_value(value: str | list[str] | None) -> str:
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def sync_geolocation_from_query_params() -> None:
    lat_raw = _first_query_value(st.query_params.get("geo_lat"))
    lon_raw = _first_query_value(st.query_params.get("geo_lon"))
    acc_raw = _first_query_value(st.query_params.get("geo_acc"))
    ts_raw = _first_query_value(st.query_params.get("geo_ts"))

    try:
        latitude = float(lat_raw)
        longitude = float(lon_raw)
    except (TypeError, ValueError):
        return

    accuracy = None
    try:
        accuracy = float(acc_raw)
    except (TypeError, ValueError):
        pass

    st.session_state.current_location = {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": accuracy,
        "geo_timestamp": ts_raw or datetime.now().isoformat(timespec="seconds"),
    }


def inject_hidden_geolocation_collector() -> None:
    components.html(
        """
        <script>
            (function() {
                const parentWin = window.parent;
                if (!parentWin) {
                    return;
                }

                function tuneInputBehavior() {
                    const numericLabels = [
                        "Numero de Repeticoes",
                        "Numero de Tratamentos",
                        "Numero de Subamostras por parcela"
                    ];

                    numericLabels.forEach((label) => {
                        const el = parentWin.document.querySelector(`input[aria-label="${label}"]`);
                        if (el) {
                            el.setAttribute("inputmode", "numeric");
                            el.setAttribute("pattern", "[0-9]*");
                            el.setAttribute("enterkeyhint", "done");
                        }
                    });

                    const upperLabels = ["Nome do Ensaio", "Praga / Alvo Avaliado"];
                    upperLabels.forEach((label) => {
                        const el = parentWin.document.querySelector(`input[aria-label="${label}"]`);
                        if (el) {
                            el.setAttribute("autocapitalize", "characters");
                            el.style.textTransform = "uppercase";
                        }
                    });

                    function patchCameraInputs(doc) {
                        if (!doc) {
                            return;
                        }

                        const fileInputs = doc.querySelectorAll('input[type="file"]');
                        fileInputs.forEach((input) => {
                            const accept = (input.getAttribute("accept") || "").toLowerCase();
                            if (!accept.includes("image")) {
                                return;
                            }

                            input.setAttribute("capture", "environment");
                            input.setAttribute("accept", "image/*");
                        });
                    }

                    patchCameraInputs(parentWin.document);
                    patchCameraInputs(document);
                }

                function isFrontStream(stream) {
                    const track = stream.getVideoTracks ? stream.getVideoTracks()[0] : null;
                    if (!track) {
                        return false;
                    }
                    const settings = typeof track.getSettings === "function" ? track.getSettings() : {};
                    const label = (track.label || "").toLowerCase();
                    const facingMode = String(settings.facingMode || "").toLowerCase();
                    if (facingMode === "environment") {
                        return false;
                    }
                    if (facingMode === "user") {
                        return true;
                    }
                    return /front|frontal|face/.test(label) && !/back|rear|traseira/.test(label);
                }

                function applyQualityConstraints(track) {
                    if (!track || typeof track.getCapabilities !== "function") {
                        return;
                    }
                    try {
                        const caps = track.getCapabilities();
                        const extra = {};
                        const advanced = [];

                        if (caps.width && typeof caps.width.max === "number") {
                            extra.width = { ideal: caps.width.max };
                        }
                        if (caps.height && typeof caps.height.max === "number") {
                            extra.height = { ideal: caps.height.max };
                        }
                        if (Array.isArray(caps.focusMode) && caps.focusMode.includes("continuous")) {
                            advanced.push({ focusMode: "continuous" });
                        }
                        if (caps.zoom && typeof caps.zoom.min === "number") {
                            advanced.push({ zoom: caps.zoom.min });
                        }
                        if (advanced.length) {
                            extra.advanced = advanced;
                        }
                        if (Object.keys(extra).length) {
                            track.applyConstraints(extra).catch(() => {});
                        }
                    } catch (e) {
                        // Alguns navegadores nao expõem capabilities completas.
                    }
                }

                async function requestRearStream(targetWin, originalGetUserMedia, hintDeviceId) {
                    const attempts = [];
                    if (hintDeviceId) {
                        attempts.push({
                            video: {
                                deviceId: { exact: hintDeviceId },
                                width: { ideal: 7680 },
                                height: { ideal: 4320 },
                            },
                        });
                    }
                    attempts.push({
                        video: {
                            facingMode: { exact: "environment" },
                            width: { ideal: 7680 },
                            height: { ideal: 4320 },
                        },
                    });
                    attempts.push({
                        video: {
                            facingMode: { ideal: "environment" },
                            width: { ideal: 7680 },
                            height: { ideal: 4320 },
                        },
                    });

                    for (const attemptConstraints of attempts) {
                        try {
                            return await originalGetUserMedia(attemptConstraints);
                        } catch (err) {
                            // Tenta a proxima estrategia.
                        }
                    }
                    return null;
                }

                function patchMediaConstraints(targetWin) {
                    if (!targetWin || targetWin.__fotoAppMediaPatch) {
                        return;
                    }
                    if (!targetWin.navigator?.mediaDevices?.getUserMedia) {
                        return;
                    }

                    const originalGetUserMedia = targetWin.navigator.mediaDevices.getUserMedia.bind(
                        targetWin.navigator.mediaDevices
                    );
                    targetWin.__fotoAppOriginalGetUserMedia = originalGetUserMedia;

                    // Alguns navegadores (ex.: certos WebViews/iOS) expoem getUserMedia como
                    // propriedade somente-leitura; sem o try/catch, essa atribuicao lancaria
                    // um erro sincrono que interromperia todo o restante do script (inclusive
                    // a solicitacao de geolocalizacao mais abaixo).
                    try {
                        targetWin.navigator.mediaDevices.getUserMedia = async function(constraints) {
                            let patched = constraints;
                            if (constraints && typeof constraints === "object" && constraints.video) {
                                const baseVideo = constraints.video === true ? {} : constraints.video;
                                if (typeof baseVideo === "object") {
                                    patched = { ...constraints, video: { ...baseVideo } };
                                    if (!patched.video.facingMode) {
                                        patched.video.facingMode = { ideal: "environment" };
                                    }
                                    if (!patched.video.width) {
                                        patched.video.width = { ideal: 7680 };
                                    }
                                    if (!patched.video.height) {
                                        patched.video.height = { ideal: 4320 };
                                    }
                                }
                            }

                            const stream = await originalGetUserMedia(patched);
                            const track = stream.getVideoTracks ? stream.getVideoTracks()[0] : null;
                            applyQualityConstraints(track);
                            return stream;
                        };
                        targetWin.__fotoAppMediaPatch = true;
                    } catch (e) {
                        // Navegador nao permite sobrescrever getUserMedia; seguimos sem o patch.
                    }
                }

                async function enforceRearCameraOnVideos(targetWin) {
                    const originalGetUserMedia = targetWin.__fotoAppOriginalGetUserMedia;
                    if (!originalGetUserMedia) {
                        return;
                    }

                    const videos = Array.from(targetWin.document.querySelectorAll("video"));
                    for (const video of videos) {
                        const stream = video.srcObject;
                        if (!stream || typeof stream.getVideoTracks !== "function") {
                            continue;
                        }
                        const tracks = stream.getVideoTracks();
                        if (!tracks.length) {
                            continue;
                        }

                        if (!isFrontStream(stream)) {
                            if (video.dataset.fotoAppQualityApplied !== "1") {
                                video.dataset.fotoAppQualityApplied = "1";
                                applyQualityConstraints(tracks[0]);
                            }
                            continue;
                        }

                        if (video.dataset.fotoAppRearSwap === "1") {
                            continue;
                        }
                        video.dataset.fotoAppRearSwap = "1";

                        const devices = await targetWin.navigator.mediaDevices
                            .enumerateDevices()
                            .catch(() => []);
                        const rearDevice = devices.find(
                            (d) =>
                                d.kind === "videoinput" &&
                                /back|rear|traseira|environment/i.test(d.label || "")
                        );

                        const rearStream = await requestRearStream(
                            targetWin,
                            originalGetUserMedia,
                            rearDevice ? rearDevice.deviceId : null
                        );

                        if (rearStream && !isFrontStream(rearStream)) {
                            tracks.forEach((t) => t.stop());
                            video.srcObject = rearStream;
                            applyQualityConstraints(rearStream.getVideoTracks()[0]);
                        } else if (rearStream) {
                            rearStream.getTracks().forEach((t) => t.stop());
                            video.dataset.fotoAppRearSwap = "0";
                        } else {
                            video.dataset.fotoAppRearSwap = "0";
                        }
                    }
                }

                function attachTapToFocus(targetWin) {
                    if (targetWin.__fotoAppTapFocusAttached) {
                        return;
                    }
                    targetWin.__fotoAppTapFocusAttached = true;

                    targetWin.document.addEventListener(
                        "pointerdown",
                        async (evt) => {
                            const video = evt.target && evt.target.closest ? evt.target.closest("video") : null;
                            if (!video || !video.srcObject) {
                                return;
                            }

                            const track = video.srcObject.getVideoTracks
                                ? video.srcObject.getVideoTracks()[0]
                                : null;
                            if (!track || typeof track.applyConstraints !== "function") {
                                return;
                            }

                            const rect = video.getBoundingClientRect();
                            const relX = Math.min(Math.max((evt.clientX - rect.left) / rect.width, 0), 1);
                            const relY = Math.min(Math.max((evt.clientY - rect.top) / rect.height, 0), 1);

                            const marker = targetWin.document.createElement("div");
                            marker.style.cssText =
                                "position:fixed;width:56px;height:56px;border:3px solid #fbbf24;" +
                                "border-radius:50%;pointer-events:none;z-index:999999;" +
                                "transform:translate(-50%,-50%);transition:opacity 0.4s ease;" +
                                `left:${evt.clientX}px;top:${evt.clientY}px;`;
                            targetWin.document.body.appendChild(marker);
                            setTimeout(() => {
                                marker.style.opacity = "0";
                                setTimeout(() => marker.remove(), 400);
                            }, 500);

                            try {
                                const caps =
                                    typeof track.getCapabilities === "function" ? track.getCapabilities() : {};
                                const advanced = [{ pointsOfInterest: [{ x: relX, y: relY }] }];
                                if (Array.isArray(caps.focusMode) && caps.focusMode.includes("single-shot")) {
                                    advanced.push({ focusMode: "single-shot" });
                                } else if (Array.isArray(caps.focusMode) && caps.focusMode.includes("continuous")) {
                                    advanced.push({ focusMode: "continuous" });
                                }
                                await track.applyConstraints({ advanced }).catch(() => {});
                            } catch (e) {
                                // Ajuste de foco por toque nao suportado neste navegador/dispositivo.
                            }
                        },
                        { passive: true }
                    );
                }

                try {
                    patchMediaConstraints(parentWin);
                    patchMediaConstraints(window);
                    attachTapToFocus(parentWin);

                    if (!parentWin.__fotoAppRearIntervalStarted) {
                        parentWin.__fotoAppRearIntervalStarted = true;
                        parentWin.setInterval(() => enforceRearCameraOnVideos(parentWin), 700);
                    }

                    const rearObserver = new MutationObserver(() => {
                        enforceRearCameraOnVideos(parentWin);
                    });
                    rearObserver.observe(parentWin.document.body, { childList: true, subtree: true });
                } catch (e) {
                    // Falha ao configurar reforco de camera traseira; nao deve bloquear o resto do script.
                }

                tuneInputBehavior();
                for (let i = 1; i <= 12; i += 1) {
                    setTimeout(tuneInputBehavior, i * 350);
                }

                const geoApi =
                    (parentWin.navigator && parentWin.navigator.geolocation)
                    || navigator.geolocation;

                if (!geoApi) {
                    return;
                }

                const now = Date.now();
                const throttleKey = "foto_streamlit_geo_last_try_ms";
                const lastTry = Number(parentWin.localStorage.getItem(throttleKey) || "0");
                const existingUrl = new URL(parentWin.location.href);
                const hasGeoInUrl =
                    existingUrl.searchParams.has("geo_lat")
                    && existingUrl.searchParams.has("geo_lon");

                if (hasGeoInUrl && now - lastTry < 7000) {
                    return;
                }

                parentWin.localStorage.setItem(throttleKey, String(now));

                geoApi.getCurrentPosition(
                    function(pos) {
                        const lat = pos.coords.latitude.toFixed(7);
                        const lon = pos.coords.longitude.toFixed(7);
                        const acc = Math.round(pos.coords.accuracy || 0).toString();

                        const url = new URL(parentWin.location.href);
                        const oldLat = url.searchParams.get("geo_lat");
                        const oldLon = url.searchParams.get("geo_lon");
                        const oldAcc = url.searchParams.get("geo_acc");

                        if (oldLat === lat && oldLon === lon && oldAcc === acc) {
                            return;
                        }

                        url.searchParams.set("geo_lat", lat);
                        url.searchParams.set("geo_lon", lon);
                        url.searchParams.set("geo_acc", acc);
                        url.searchParams.set("geo_ts", String(Date.now()));
                        try {
                            parentWin.history.replaceState({}, "", url.toString());
                        } catch (e) {
                            // Alguns navegadores podem restringir alteracoes de URL neste contexto.
                        }
                    },
                    function() {
                        // Sem permissao de GPS ou indisponivel no dispositivo.
                    },
                    {
                        enableHighAccuracy: true,
                        maximumAge: 5000,
                        timeout: 6000
                    }
                );
            })();
        </script>
        """,
        height=0,
        width=0,
    )


def build_photo_metadata(item: dict, location: dict | None) -> dict:
    latitude = None
    longitude = None
    accuracy_m = None
    geo_timestamp = None

    if location:
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        accuracy_m = location.get("accuracy_m")
        geo_timestamp = location.get("geo_timestamp")

    return {
        "repeticao": item["repeticao"],
        "tratamento": item["tratamento"],
        "parcela": item["parcela"],
        "subamostra": item["subamostra"],
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": accuracy_m,
        "geo_timestamp": geo_timestamp,
    }


def inject_exif_description_jpeg(image_bytes: bytes, description: str) -> bytes:
    # Insere APP1 EXIF simples com ImageDescription e Software.
    if not image_bytes.startswith(b"\xff\xd8"):
        return image_bytes

    desc_bytes = description.encode("ascii", "ignore") + b"\x00"
    software_bytes = b"FotoStreamlitApp\x00"

    entry_count = 2
    ifd_size = 2 + (entry_count * 12) + 4
    data_start = 8 + ifd_size
    desc_offset = data_start
    software_offset = desc_offset + len(desc_bytes)

    tiff = io.BytesIO()
    tiff.write(b"II")
    tiff.write(struct.pack("<H", 42))
    tiff.write(struct.pack("<I", 8))
    tiff.write(struct.pack("<H", entry_count))

    tiff.write(struct.pack("<HHII", 0x010E, 2, len(desc_bytes), desc_offset))
    tiff.write(struct.pack("<HHII", 0x0131, 2, len(software_bytes), software_offset))
    tiff.write(struct.pack("<I", 0))

    tiff.write(desc_bytes)
    tiff.write(software_bytes)

    exif_payload = b"Exif\x00\x00" + tiff.getvalue()
    segment = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload
    return image_bytes[:2] + segment + image_bytes[2:]


def embed_photo_metadata(image_bytes: bytes, mime: str, photo_metadata: dict) -> bytes:
    metadata_text = json.dumps(photo_metadata, ensure_ascii=True, separators=(",", ":"))

    if mime == "image/jpeg" or image_bytes.startswith(b"\xff\xd8"):
        return inject_exif_description_jpeg(image_bytes, metadata_text)

    return image_bytes


def build_walk_geojson(points: list[dict]) -> dict:
    features = []

    if len(points) >= 2:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[p["longitude"], p["latitude"]] for p in points],
                },
                "properties": {"descricao": "Caminhamento do operador"},
            }
        )

    for p in points:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [p["longitude"], p["latitude"]],
                },
                "properties": {
                    "indice": p["indice"],
                    "repeticao": p["repeticao"],
                    "tratamento": p["tratamento"],
                    "parcela": p["parcela"],
                    "subamostra": p["subamostra"],
                    "accuracy_m": p.get("accuracy_m"),
                    "capturado_em": p.get("capturado_em"),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def build_walk_map_html(points: list[dict]) -> str:
    if not points:
        return """<!doctype html><html><body><h3>Sem coordenadas para mapear.</h3></body></html>"""

    width = 920
    height = 620
    margin = 40

    min_lat = min(p["latitude"] for p in points)
    max_lat = max(p["latitude"] for p in points)
    min_lon = min(p["longitude"] for p in points)
    max_lon = max(p["longitude"] for p in points)

    lat_span = max(max_lat - min_lat, 0.000001)
    lon_span = max(max_lon - min_lon, 0.000001)

    def project(lat: float, lon: float) -> tuple[float, float]:
        x = margin + ((lon - min_lon) / lon_span) * (width - 2 * margin)
        y = height - margin - ((lat - min_lat) / lat_span) * (height - 2 * margin)
        return x, y

    svg_points = []
    point_labels = []
    for p in points:
        x, y = project(p["latitude"], p["longitude"])
        svg_points.append(f"{x:.2f},{y:.2f}")
        point_labels.append(
            f"<circle cx='{x:.2f}' cy='{y:.2f}' r='5' fill='#dc2626' />"
            f"<text x='{x + 8:.2f}' y='{y - 8:.2f}' font-size='12' fill='#111827'>#{p['indice']}</text>"
        )

    table_rows = []
    for p in points:
        table_rows.append(
            "<tr>"
            f"<td>{p['indice']}</td>"
            f"<td>{p['repeticao']}</td>"
            f"<td>{p['tratamento']}</td>"
            f"<td>{p['parcela']}</td>"
            f"<td>{p['subamostra']}</td>"
            f"<td>{p['latitude']:.7f}</td>"
            f"<td>{p['longitude']:.7f}</td>"
            f"<td>{'' if p.get('accuracy_m') is None else p.get('accuracy_m')}</td>"
            "</tr>"
        )

    polyline = " ".join(svg_points)
    labels = "".join(point_labels)
    rows = "".join(table_rows)

    return f"""
<!doctype html>
<html>
<head>
  <meta charset='utf-8' />
  <title>Mapa de Caminhamento</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 16px; }}
    h2 {{ margin-bottom: 8px; }}
    .card {{ border: 1px solid #d1d5db; border-radius: 12px; padding: 12px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 13px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 6px; text-align: center; }}
    th {{ background: #f3f4f6; }}
  </style>
</head>
<body>
  <h2>Mapa de Caminhamento (GPS das fotos)</h2>
  <div class='card'>
    <svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' role='img' aria-label='Mapa de caminhamento'>
      <rect x='1' y='1' width='{width - 2}' height='{height - 2}' fill='#ffffff' stroke='#d1d5db' />
      <polyline points='{polyline}' fill='none' stroke='#2563eb' stroke-width='2.5' />
      {labels}
    </svg>
  </div>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Rep</th><th>Trat</th><th>Parcela</th><th>Sub</th><th>Latitude</th><th>Longitude</th><th>Precisao (m)</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</body>
</html>
"""


def sanitize_token(text: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_only).strip("_")
    return cleaned or fallback


def photo_filename(item: dict, index: int) -> str:
    return (
        f"{index + 1:04d}_R{item['repeticao']:02d}_"
        f"T{item['tratamento']:02d}_P{item['parcela']:03d}_S{item['subamostra']:02d}.jpg"
    )


def build_zip_bytes(setup: dict, sequence: list[dict], captures: dict) -> tuple[bytes, str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ensaio_token = sanitize_token(setup["ensaio"], "ensaio")
    zip_name = f"coleta_{ensaio_token}_{ts}.zip"

    zip_buffer = io.BytesIO()

    with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zf:
        metadata = {
            "ensaio": setup["ensaio"],
            "alvo": setup["alvo"],
            "repeticoes": setup["repeticoes"],
            "tratamentos": setup["tratamentos"],
            "subamostras": setup["subamostras"],
            "total_itens": len(sequence),
            "fotos_capturadas": len(captures),
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "ordem": ["repeticao", "tratamento", "subamostra"],
        }
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))

        walk_points = []

        manifest_stream = io.StringIO()
        writer = csv.DictWriter(
            manifest_stream,
            fieldnames=[
                "indice",
                "repeticao",
                "tratamento",
                "parcela",
                "subamostra",
                "arquivo",
                "status",
                "capturado_em",
                "latitude",
                "longitude",
                "accuracy_m",
                "geo_timestamp",
            ],
        )
        writer.writeheader()

        for idx, item in enumerate(sequence):
            key = str(idx)
            captured = captures.get(key)
            archive_name = ""
            captured_at = ""
            status = "pendente"

            if captured:
                archive_name = photo_filename(item, idx)
                captured_at = captured.get("timestamp", "")
                status = "capturada"
                zf.writestr(f"fotos/{archive_name}", captured["bytes"])
                zf.writestr(
                    f"fotos/{archive_name}.metadata.json",
                    json.dumps(captured.get("metadata", {}), ensure_ascii=False, indent=2),
                )

            photo_meta = captured.get("metadata", {}) if captured else {}
            lat = photo_meta.get("latitude")
            lon = photo_meta.get("longitude")

            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                walk_points.append(
                    {
                        "indice": idx + 1,
                        "repeticao": item["repeticao"],
                        "tratamento": item["tratamento"],
                        "parcela": item["parcela"],
                        "subamostra": item["subamostra"],
                        "latitude": lat,
                        "longitude": lon,
                        "accuracy_m": photo_meta.get("accuracy_m"),
                        "capturado_em": captured_at,
                    }
                )

            writer.writerow(
                {
                    "indice": idx + 1,
                    "repeticao": item["repeticao"],
                    "tratamento": item["tratamento"],
                    "parcela": item["parcela"],
                    "subamostra": item["subamostra"],
                    "arquivo": archive_name,
                    "status": status,
                    "capturado_em": captured_at,
                    "latitude": photo_meta.get("latitude"),
                    "longitude": photo_meta.get("longitude"),
                    "accuracy_m": photo_meta.get("accuracy_m"),
                    "geo_timestamp": photo_meta.get("geo_timestamp"),
                }
            )

        zf.writestr("manifesto.csv", manifest_stream.getvalue().encode("utf-8"))
        zf.writestr(
            "caminhamento/pontos_captura.geojson",
            json.dumps(build_walk_geojson(walk_points), ensure_ascii=False, indent=2),
        )
        zf.writestr("caminhamento/mapa_caminhamento.html", build_walk_map_html(walk_points))

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), zip_name


# ---------------------------
# Interface
# ---------------------------
def inject_mobile_styles() -> None:
    st.markdown(
        """
        <style>
            :root {
                --card-bg: #f9fcfa;
                --card-border: #dce8e1;
                --form-bg: #fbfcfb;
                --form-border: #d7dfd8;
                --cta-bg: #0e7490;
                --cta-fg: #ffffff;
                --cta-border: #0e7490;
            }
            @media (prefers-color-scheme: dark) {
                :root {
                    --card-bg: #1f2937;
                    --card-border: #4b5563;
                    --form-bg: #111827;
                    --form-border: #4b5563;
                    --cta-bg: #38bdf8;
                    --cta-fg: #0b1220;
                    --cta-border: #38bdf8;
                }
            }
            .block-container {
                max-width: 760px;
                padding-top: 1rem;
                padding-bottom: 4rem;
            }
            .progress-card {
                border: 1px solid var(--card-border);
                border-radius: 14px;
                padding: 0.9rem;
                background: var(--card-bg);
                margin-bottom: 0.8rem;
            }
            .stButton > button,
            .stDownloadButton > button {
                width: 100%;
                min-height: 3rem;
                font-size: 1.02rem;
                border-radius: 12px;
            }
            [data-testid="stFormSubmitButton"] button {
                min-height: 3.2rem;
                width: 100%;
                border-radius: 12px;
                border: 1px solid var(--cta-border);
                background: var(--cta-bg);
                color: var(--cta-fg);
                font-weight: 700;
            }
            [data-testid="stFormSubmitButton"] button:disabled {
                opacity: 0.7;
                border-style: dashed;
            }
            [data-testid="stForm"] {
                border: 1px solid var(--form-border);
                border-radius: 14px;
                padding: 1rem;
                background-color: var(--form-bg);
            }
            @media (max-width: 640px) {
                h1 {font-size: 1.6rem;}
                h2 {font-size: 1.35rem;}
                h3 {font-size: 1.15rem;}
                .stButton > button,
                .stDownloadButton > button {
                    min-height: 3.2rem;
                    font-size: 1.05rem;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_setup_form() -> None:
    st.header("1) Configuracao inicial")

    ensaio = st.text_input(
        "Nome do Ensaio",
        key="cfg_ensaio",
        on_change=force_uppercase_field,
        args=("cfg_ensaio",),
    )
    alvo = st.text_input(
        "Praga / Alvo Avaliado",
        key="cfg_alvo",
        on_change=force_uppercase_field,
        args=("cfg_alvo",),
    )

    ensaio_up = ensaio.upper()
    alvo_up = alvo.upper()
    if ensaio != ensaio_up:
        st.session_state.cfg_ensaio = ensaio_up
        st.rerun()
    if alvo != alvo_up:
        st.session_state.cfg_alvo = alvo_up
        st.rerun()

    ensaio = ensaio_up
    alvo = alvo_up

    repeticoes = int(
        st.number_input(
            "Numero de Repeticoes",
            min_value=0,
            max_value=MAX_REPETICOES,
            step=1,
            key="cfg_repeticoes",
        )
    )
    tratamentos = int(
        st.number_input(
            "Numero de Tratamentos",
            min_value=0,
            max_value=MAX_TRATAMENTOS,
            step=1,
            key="cfg_tratamentos",
        )
    )
    subamostras = int(
        st.number_input(
            "Numero de Subamostras por parcela",
            min_value=0,
            max_value=MAX_SUBAMOSTRAS,
            step=1,
            key="cfg_subamostras",
        )
    )

    errors, total_parcelas, total_fotos = validate_setup(
        ensaio=ensaio,
        alvo=alvo,
        repeticoes=repeticoes,
        tratamentos=tratamentos,
        subamostras=subamostras,
    )

    summary_col_info, summary_col_btn = st.columns([2, 1])
    with summary_col_info:
        st.markdown("**Resumo automatico**")
    with summary_col_btn:
        should_start = st.button(
            "Iniciar avaliacao",
            type="primary",
            disabled=bool(errors),
            use_container_width=True,
        )

    st.write(f"Parcelas: {total_parcelas}")
    st.write(f"Total de fotos: {total_fotos}")
    st.write("Ordem de percurso: repeticao -> tratamento -> subamostra")
    st.caption("A coleta so inicia quando voce tocar em 'Iniciar avaliacao'.")

    if errors:
        for err in errors:
            st.error(err)

    if should_start and not errors:
        st.session_state.setup = {
            "ensaio": ensaio.strip().upper(),
            "alvo": alvo.strip().upper(),
            "repeticoes": repeticoes,
            "tratamentos": tratamentos,
            "subamostras": subamostras,
        }
        st.session_state.sequence = build_sequence(repeticoes, tratamentos, subamostras)
        st.session_state.current_idx = 0
        st.session_state.captures = {}
        st.session_state.retake_counter = 0
        st.session_state.zip_cache = None
        st.session_state.zip_name = ""
        st.session_state.auto_started_signature = ""
        st.session_state.flow_started = True
        st.rerun()


def render_capture_flow() -> None:
    setup = st.session_state.setup
    sequence = st.session_state.sequence
    captures = st.session_state.captures

    total = len(sequence)
    current_idx = st.session_state.current_idx

    st.header("2) Coleta sequencial")
    st.markdown(
        '<div class="progress-card">'
        f"<b>Ensaio:</b> {setup['ensaio']}<br>"
        f"<b>Alvo:</b> {setup['alvo']}<br>"
        f"<b>Fotos confirmadas:</b> {len(captures)} de {total}"
        "</div>",
        unsafe_allow_html=True,
    )

    st.progress(min(len(captures) / total, 1.0) if total else 0.0)

    if current_idx >= total:
        st.success("Coleta concluida. Todas as fotos foram confirmadas.")

        if st.session_state.zip_cache is None:
            zip_bytes, zip_name = build_zip_bytes(setup, sequence, captures)
            st.session_state.zip_cache = zip_bytes
            st.session_state.zip_name = zip_name

        st.download_button(
            "Baixar ZIP da coleta",
            data=st.session_state.zip_cache,
            file_name=st.session_state.zip_name,
            mime="application/zip",
            type="primary",
        )

        return

    item = sequence[current_idx]
    item_key = str(current_idx)

    st.subheader(f"Proxima foto: {current_idx + 1} de {total}")
    st.write(f"Repeticao: {item['repeticao']}")
    st.write(f"Tratamento: {item['tratamento']}")
    st.write(f"Parcela: {item['parcela']}")
    st.write(f"Subamostra: {item['subamostra']}")

    if item_key in captures:
        st.success("Foto desta subamostra ja foi confirmada.")
        st.image(captures[item_key]["bytes"], use_container_width=True)

        col_refazer, col_avancar = st.columns(2)
        with col_refazer:
            if st.button("Refazer foto atual"):
                del st.session_state.captures[item_key]
                st.session_state.zip_cache = None
                st.session_state.retake_counter += 1
                st.rerun()
        with col_avancar:
            if st.button("Avancar"):
                st.session_state.current_idx += 1
                st.session_state.retake_counter += 1
                st.rerun()
    else:
        # Key muda a cada item/retake para o widget nao reter a foto do item anterior.
        camera_key = f"cam_{current_idx}_{st.session_state.retake_counter}"
        picture = st.camera_input("Capture a foto desta subamostra", key=camera_key)

        if picture is not None:
            raw_image_bytes = picture.getvalue()
            photo_metadata = build_photo_metadata(
                item=item,
                location=st.session_state.current_location,
            )
            image_bytes = embed_photo_metadata(raw_image_bytes, picture.type or "", photo_metadata)
            st.image(raw_image_bytes, caption="Pre-visualizacao", use_container_width=True)
            if st.button("Confirmar foto e avancar", type="primary"):
                st.session_state.captures[item_key] = {
                    "bytes": image_bytes,
                    "mime": picture.type or "image/jpeg",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "metadata": photo_metadata,
                }
                st.session_state.current_idx += 1
                st.session_state.zip_cache = None
                st.session_state.retake_counter += 1
                st.rerun()

    st.button(
        "Voltar 1 item",
        disabled=current_idx == 0,
        on_click=lambda: st.session_state.update(
            {
                "current_idx": max(0, current_idx - 1),
                "retake_counter": st.session_state.retake_counter + 1,
            }
        ),
    )


# ---------------------------
# Execucao principal
# ---------------------------
def main() -> None:
    st.set_page_config(
        page_title="Coletor Sequencial de Fotos",
        page_icon="📷",
        layout="centered",
    )
    inject_mobile_styles()
    init_session_state()
    sync_geolocation_from_query_params()
    inject_hidden_geolocation_collector()

    st.title("Coletor Sequencial de Fotos de Campo")
    st.caption(
        "Aplicativo mobile-first para capturar fotos em sequencia de parcelas e subamostras."
    )

    st.warning(
        "As fotos ficam somente na sessao atual. Elas podem ser perdidas se a pagina for "
        "atualizada, fechada ou se a sessao expirar."
    )

    if not st.session_state.flow_started:
        render_setup_form()
    else:
        render_capture_flow()


if __name__ == "__main__":
    main()
