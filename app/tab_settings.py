"""Settings tab — manage machine profiles and global config via the UI."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.config import MachineProfile, load_settings, save_settings

_PROJECT_ROOT = Path(__file__).parent.parent


def _list_prompts_by_prefix(prefix: str) -> list[str]:
    """Return all .md files in prompts/ whose filename starts with *prefix*."""
    return sorted(
        str(p.relative_to(_PROJECT_ROOT))
        for p in (_PROJECT_ROOT / "prompts").glob(f"{prefix}*.md")
    )


def _list_extraction_prompts() -> list[str]:
    return _list_prompts_by_prefix("extraction")


def _list_refinement_prompts() -> list[str]:
    return _list_prompts_by_prefix("refinement")


def run() -> None:
    """Render the Settings tab."""
    if st.session_state.pop("settings_saved_toast", False):
        st.toast("Settings saved to `src/config.json`.", icon="✅")

    st.subheader("Settings")
    st.caption(
        "All values are persisted in `src/config.json`. "
        "Changes take effect on the next execution. "
        "CLI flags and sidebar selections override these defaults at runtime."
    )

    cfg = load_settings()

    # ── Machine management ────────────────────────────────────────────────────
    st.markdown("#### Machine Profiles")
    st.caption(
        "Each machine profile stores its own Vertex AI settings. "
        "Select the active machine in the sidebar."
    )

    machine_names = [m.name for m in cfg.machines]
    active_idx = machine_names.index(cfg.active_machine) if cfg.active_machine in machine_names else 0

    # Pick which machine to edit
    edit_machine_name: str = st.selectbox(
        "Edit machine", machine_names, index=active_idx, key="settings_edit_machine"
    )
    edit_machine = next((m for m in cfg.machines if m.name == edit_machine_name), cfg.machines[0])

    mc1, mc2 = st.columns([1, 1])
    with mc1:
        if st.button("➕ Add new machine", key="btn_add_machine"):
            new_name = f"Machine {len(cfg.machines) + 1}"
            cfg.machines.append(MachineProfile(name=new_name))
            save_settings(cfg)
            st.session_state["settings_saved_toast"] = True
            st.rerun()
    with mc2:
        if len(cfg.machines) > 1:
            if st.button(f"🗑️ Remove '{edit_machine_name}'", key="btn_remove_machine"):
                cfg.machines = [m for m in cfg.machines if m.name != edit_machine_name]
                if cfg.active_machine == edit_machine_name:
                    cfg.active_machine = cfg.machines[0].name
                save_settings(cfg)
                st.session_state["settings_saved_toast"] = True
                st.rerun()

    st.markdown("---")

    # ── Machine settings form ─────────────────────────────────────────────────
    with st.form("settings_form"):
        st.markdown(f"#### Vertex AI — *{edit_machine_name}*")

        s0 = st.text_input(
            "Machine name",
            value=edit_machine.name,
            help="Label shown in the sidebar machine selector.",
        )

        s1, s2, s3 = st.columns([2, 2, 2])
        with s1:
            new_project_id = st.text_input(
                "Project ID", value=edit_machine.project_id,
                help="Google Cloud project ID for this machine.",
            )
            new_auth_mode: str = st.selectbox(
                "Auth Mode", ["api", "gcloud"],
                index=0 if edit_machine.auth_mode == "api" else 1,
            )
        with s2:
            new_location = st.text_input("Location", value=edit_machine.location)
            _model_opts = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-lite-preview"]
            new_model: str = st.selectbox(
                "Model",
                _model_opts,
                index=_model_opts.index(edit_machine.model) if edit_machine.model in _model_opts else 0,
            )
        with s3:
            new_refine = st.number_input(
                "Refinement Passes", min_value=0, value=edit_machine.refine_iterations, step=1
            )
            new_cse = st.number_input(
                "Max Errors (CLEAN)", min_value=-1, value=edit_machine.clean_stop_max_errors, step=1
            )
            new_diminishing_returns = st.checkbox(
                "Enable diminishing returns stop",
                value=edit_machine.diminishing_returns_enabled,
                help=(
                    "When enabled (default), refinement stops early if successive passes show no "
                    "reduction in errors."
                ),
            )

        _ext_prompts = _list_extraction_prompts()
        _ref_prompts = _list_refinement_prompts()
        s4, s5 = st.columns([3, 3])
        with s4:
            new_ext_prompt: str = st.selectbox(
                "Extraction Prompt", _ext_prompts,
                index=_ext_prompts.index(edit_machine.extraction_prompt)
                if edit_machine.extraction_prompt in _ext_prompts else 0,
            )
        with s5:
            new_ref_prompt: str = st.selectbox(
                "Refinement Prompt", _ref_prompts,
                index=_ref_prompts.index(edit_machine.refinement_prompt)
                if edit_machine.refinement_prompt in _ref_prompts else 0,
            )

        st.markdown("---")
        st.markdown("#### Processing")
        p1, p2, p3 = st.columns([2, 2, 2])
        with p1:
            new_chunk_size = st.number_input(
                "Chunk Size (pages)", min_value=0, value=cfg.processing.chunk_size, step=5
            )
        with p2:
            new_chunk_overlap = st.number_input(
                "Chunk Overlap (pages)", min_value=0, value=cfg.processing.chunk_overlap, step=1
            )
        with p3:
            new_workers = st.number_input(
                "Workers", min_value=1, value=cfg.processing.workers, step=1
            )

        new_validate = st.checkbox(
            "Validate after convert by default",
            value=cfg.processing.validate_after_convert,
            help=(
                "When enabled, the **Validate after convert** checkbox in the Convert File "
                "and Batch Convert tabs will be pre-checked. Validation runs a post-conversion "
                "quality check on the output markdown to detect structural issues."
            ),
        )

        st.markdown("---")
        st.markdown("#### Batch")
        b1, b2 = st.columns([2, 4])
        with b1:
            new_recursive = st.checkbox("Recursive folder scan", value=cfg.batch.recursive)
        with b2:
            new_extensions = st.text_input(
                "File extensions (comma-separated)",
                value=", ".join(cfg.batch.extensions),
                help='e.g. ".pdf, .PDF"',
            )

        st.markdown("---")
        st.markdown("#### Logging")
        st.caption("Execution log (structured JSONL) paths.")
        l1, l2 = st.columns([2, 4])
        with l1:
            new_exec_log_dir = st.text_input(
                "Exec log directory", value=cfg.logging.exec_log_dir,
                help="Folder for execution JSONL logs.",
            )
        with l2:
            new_exec_log_file = st.text_input(
                "Exec log filename", value=cfg.logging.exec_log_file,
                help="Filename inside the exec log directory.",
            )
        st.caption("Application rotating file logs (see `src/logging_config.py`).")
        l3, l4, l5 = st.columns([2, 2, 2])
        with l3:
            new_app_log_dir = st.text_input(
                "App log directory", value=cfg.logging.log_dir,
                help="Folder for rotating app log files.",
            )
        with l4:
            new_log_max_bytes = st.number_input(
                "Log max bytes (per file)",
                min_value=1024,
                value=int(cfg.logging.log_max_bytes),
                step=1048576,
                help="Rotate when a log file reaches this size (bytes).",
            )
        with l5:
            new_log_backup_count = st.number_input(
                "Log backup count",
                min_value=0,
                value=int(cfg.logging.log_backup_count),
                step=1,
                help="Number of rotated log files to keep.",
            )

        st.markdown("---")
        submitted = st.form_submit_button("💾 Save settings", type="primary")

    if submitted:
        exts = [e.strip() for e in new_extensions.split(",") if e.strip()]
        if not exts:
            exts = [".pdf"]

        # Rename machine if name changed (and avoid duplicates)
        new_name_stripped = s0.strip() or edit_machine_name
        for m in cfg.machines:
            if m.name == edit_machine_name:
                m.name = new_name_stripped
                m.project_id = new_project_id
                m.auth_mode = new_auth_mode
                m.location = new_location
                m.model = new_model
                m.refine_iterations = int(new_refine)
                m.clean_stop_max_errors = int(new_cse)
                m.diminishing_returns_enabled = new_diminishing_returns
                m.extraction_prompt = new_ext_prompt
                m.refinement_prompt = new_ref_prompt
                break

        if cfg.active_machine == edit_machine_name:
            cfg.active_machine = new_name_stripped

        cfg.processing.chunk_size = int(new_chunk_size)
        cfg.processing.chunk_overlap = int(new_chunk_overlap)
        cfg.processing.workers = int(new_workers)
        cfg.processing.validate_after_convert = new_validate

        cfg.batch.recursive = new_recursive
        cfg.batch.extensions = exts

        cfg.logging.exec_log_dir = new_exec_log_dir
        cfg.logging.exec_log_file = new_exec_log_file
        cfg.logging.log_dir = new_app_log_dir
        cfg.logging.log_max_bytes = int(new_log_max_bytes)
        cfg.logging.log_backup_count = int(new_log_backup_count)

        # Sync vertexai from the edited machine so save_settings persists correctly
        cfg.vertexai.project_id = new_project_id
        cfg.vertexai.auth_mode = new_auth_mode
        cfg.vertexai.location = new_location
        cfg.vertexai.model = new_model
        cfg.vertexai.refine_iterations = int(new_refine)
        cfg.vertexai.clean_stop_max_errors = int(new_cse)
        cfg.vertexai.diminishing_returns_enabled = new_diminishing_returns
        cfg.vertexai.extraction_prompt = new_ext_prompt
        cfg.vertexai.refinement_prompt = new_ref_prompt

        # save_settings will update active machine with vertexai values
        # but we edited a specific machine directly above, so just write all machines
        _write_all_machines(cfg)

        st.session_state["settings_saved_toast"] = True
        st.rerun()


def _write_all_machines(cfg) -> None:
    """Save all settings without remapping through active machine logic."""
    import json
    from dataclasses import asdict
    from src.config import _CONFIG_PATH

    data = {
        "active_machine": cfg.active_machine,
        "machines": [asdict(m) for m in cfg.machines],
        "processing": asdict(cfg.processing),
        "batch": asdict(cfg.batch),
        "logging": asdict(cfg.logging),
    }
    _CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
