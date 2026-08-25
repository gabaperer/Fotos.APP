import csv
import io
import json
import re
import unicodedata
from datetime import datetime
from zipfile import ZIP_DEFLATED, ZipFile

import streamlit as st

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
        "cfg_repeticoes": 1,
        "cfg_tratamentos": 1,
        "cfg_subamostras": 1,
        "flow_started": False,
        "setup": {},
        "sequence": [],
        "current_idx": 0,
        "captures": {},
        "retake_counter": 0,
        "zip_cache": None,
        "zip_name": "",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_flow(keep_config: bool = True) -> None:
    st.session_state.flow_started = False
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
        st.session_state.cfg_repeticoes = 1
        st.session_state.cfg_tratamentos = 1
        st.session_state.cfg_subamostras = 1


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
                }
            )

        zf.writestr("manifesto.csv", manifest_stream.getvalue().encode("utf-8"))

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), zip_name


# ---------------------------
# Interface
# ---------------------------
def inject_mobile_styles() -> None:
    st.markdown(
        """
        <style>
            .block-container {
                max-width: 760px;
                padding-top: 1rem;
                padding-bottom: 4rem;
            }
            .app-card {
                border: 1px solid #d9e2da;
                background: linear-gradient(180deg, #f7fbf8 0%, #ffffff 100%);
                border-radius: 16px;
                padding: 1rem;
                margin-bottom: 1rem;
            }
            .progress-card {
                border: 1px solid #dce8e1;
                border-radius: 14px;
                padding: 0.9rem;
                background: #f9fcfa;
                margin-bottom: 0.8rem;
            }
            .stButton > button,
            .stDownloadButton > button {
                width: 100%;
                min-height: 3rem;
                font-size: 1.02rem;
                border-radius: 12px;
            }
            [data-testid="stForm"] {
                border: 1px solid #d7dfd8;
                border-radius: 14px;
                padding: 1rem;
                background-color: #fbfcfb;
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

    with st.form("setup_form"):
        ensaio = st.text_input("Nome do Ensaio", key="cfg_ensaio")
        alvo = st.text_input("Praga / Alvo Avaliado", key="cfg_alvo")

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

        st.markdown("**Resumo antes de gerar**")
        st.write(f"Parcelas: {total_parcelas}")
        st.write(f"Total de fotos: {total_fotos}")
        st.write("Ordem de percurso: repeticao -> tratamento -> subamostra")

        if errors:
            for err in errors:
                st.error(err)

        submitted = st.form_submit_button(
            "Gerar Sequencia de Campo",
            type="primary",
            disabled=bool(errors),
        )

    if submitted and not errors:
        st.session_state.setup = {
            "ensaio": ensaio.strip(),
            "alvo": alvo.strip(),
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

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Nova coleta (manter configuracao)"):
                reset_flow(keep_config=True)
                st.rerun()
        with col_b:
            if st.button("Nova coleta (limpar tudo)"):
                reset_flow(keep_config=False)
                st.rerun()

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

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Refazer foto atual"):
                del st.session_state.captures[item_key]
                st.session_state.zip_cache = None
                st.session_state.retake_counter += 1
                st.rerun()
        with col2:
            if st.button("Avancar"):
                st.session_state.current_idx += 1
                st.session_state.retake_counter += 1
                st.rerun()
    else:
        camera_key = f"cam_{current_idx}_{st.session_state.retake_counter}"
        picture = st.camera_input("Capture a foto desta subamostra", key=camera_key)

        if picture is not None:
            image_bytes = picture.getvalue()
            st.image(image_bytes, caption="Pre-visualizacao", use_container_width=True)
            if st.button("Confirmar foto e avancar", type="primary"):
                st.session_state.captures[item_key] = {
                    "bytes": image_bytes,
                    "mime": picture.type or "image/jpeg",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
                st.session_state.current_idx += 1
                st.session_state.zip_cache = None
                st.session_state.retake_counter += 1
                st.rerun()

    nav_col1, nav_col2 = st.columns(2)
    with nav_col1:
        if st.button("Voltar 1 item", disabled=current_idx == 0):
            st.session_state.current_idx = max(0, current_idx - 1)
            st.session_state.retake_counter += 1
            st.rerun()
    with nav_col2:
        if st.button("Encerrar e baixar parcial"):
            zip_bytes, zip_name = build_zip_bytes(setup, sequence, captures)
            st.session_state.zip_cache = zip_bytes
            st.session_state.zip_name = zip_name
            st.rerun()

    if st.session_state.zip_cache:
        st.download_button(
            "Baixar ZIP parcial",
            data=st.session_state.zip_cache,
            file_name=st.session_state.zip_name,
            mime="application/zip",
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
