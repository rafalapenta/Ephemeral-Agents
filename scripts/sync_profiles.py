#!/usr/bin/env python3
"""
sync_profiles.py — Sincroniza os SOUL.md e configurações do repositório AGgency
para os perfis locais do Hermes (%LOCALAPPDATA%\hermes\profiles) e sandboxes Docker.
"""

import os
import sys
from pathlib import Path

# Diretórios base
REPO_ROOT = Path(__file__).resolve().parent.parent
BOTS_CONFIG_DIR = REPO_ROOT / "src" / "bots_config"
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
HERMES_PROFILES_DIR = LOCAL_APPDATA / "hermes" / "profiles"
DOCKER_SANDBOX_DIR = HERMES_PROFILES_DIR / "atlas" / "sandboxes" / "docker" / "default" / "home" / "AGgency" / "src" / "bots_config"

# Mapeamento de bots e aliases
BOT_MAPPING = {
    "atlas": "atlas",
    "ceo": "atlas",
    "vulcan": "vulcan",
    "tech-infrastructure": "vulcan",
    "aura": "aura",
    "product-spatial": "aura",
    "vesper": "vesper",
    "growth-sales": "vesper",
    "sterling": "sterling",
    "business-operations": "sterling",
    "lyra": "lyra",
    "research-verticals": "lyra",
}

def sync_profiles():
    print(f"[*] Repositório AGgency: {REPO_ROOT}")
    print(f"[*] Destino Perfis Hermes: {HERMES_PROFILES_DIR}\n")

    if not BOTS_CONFIG_DIR.exists():
        print(f"[!] Erro: Diretório fonte {BOTS_CONFIG_DIR} não encontrado.")
        sys.exit(1)

    synced_count = 0
    errors_count = 0

    # 1. Sincronizar para perfis locais do Hermes
    for source_folder, target_profile in BOT_MAPPING.items():
        source_soul = BOTS_CONFIG_DIR / source_folder / "SOUL.md"
        if not source_soul.exists():
            # tenta minúsculo
            source_soul = BOTS_CONFIG_DIR / source_folder / "soul.md"

        if not source_soul.exists():
            continue

        target_dir = HERMES_PROFILES_DIR / target_profile
        target_soul = target_dir / "SOUL.md"

        try:
            raw = source_soul.read_bytes()
            # Valida UTF-8
            text = raw.decode("utf-8")
            
            if target_dir.exists():
                target_soul.write_text(text, encoding="utf-8")
                print(f"[OK] {source_folder} -> {target_soul} ({len(text)} chars)")
                synced_count += 1
            else:
                print(f"[SKIP] Perfil de destino {target_dir} não existe localmente.")
        except UnicodeDecodeError as e:
            print(f"[ERR] Erro de decodificação UTF-8 em {source_soul}: {e}")
            errors_count += 1
        except Exception as e:
            print(f"[ERR] Falha ao copiar {source_soul} -> {target_soul}: {e}")
            errors_count += 1

    # 2. Sincronizar para sandbox Docker do Atlas se existir
    if DOCKER_SANDBOX_DIR.parent.exists():
        print(f"\n[*] Sincronizando com Sandbox Docker: {DOCKER_SANDBOX_DIR}")
        for source_folder in BOTS_CONFIG_DIR.iterdir():
            if source_folder.is_dir():
                src_file = source_folder / "SOUL.md"
                if not src_file.exists():
                    src_file = source_folder / "soul.md"
                if src_file.exists():
                    dest_dir = DOCKER_SANDBOX_DIR / source_folder.name
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_file = dest_dir / "SOUL.md"
                    text = src_file.read_text(encoding="utf-8")
                    dest_file.write_text(text, encoding="utf-8")
        print("[OK] Sandbox Docker sincronizada.")

    print(f"\n[+] Sincronização concluída: {synced_count} perfis atualizados, {errors_count} erros.")

if __name__ == "__main__":
    sync_profiles()
