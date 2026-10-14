# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks import collect_all

datas = []
hiddenimports = ['db', 'scanner', 'trader', 'polymarket_api', 'polymarket_auth', 'categorize', 'config', 'crypto15m', 'crypto15m_trader', 'crypto15m_record', 'backtest', 'indicators', 'clob_ws', 'rules', 'copy_trader', 'spot_ws', 'rtds_ws', 'replay', 'parlay_generator', 'ws_ssl', 'script_sandbox', 'script_engine', 'script_backtest', 'script_docs', 'script_audit', 'statistics', 'activity_ws', 'terminal', 'terminal_trade', 'terminal_paper', 'kalshi', 'cross_venue', 'remote', 'bot_secrets', 'bot_telegram', 'bot_discord', 'ai_analysis', 'ai_secrets', 'ai_providers', 'autopilot', 'mcp_server', 'mcp_tools', 'mcp_stdio', 'mcp_agents', 'mcp_rails', 'forecast_ledger', 'logscrub', 'health', 'perps_api', 'perps_board', 'perps_maker', 'perps_ws', 'perps_backtest', 'binance_ref', 'user_ws', 'data_v2', 'script_backtest_child']
datas += collect_data_files('eth_account')
datas += copy_metadata('eth_account')
datas += copy_metadata('eth_utils')
datas += copy_metadata('eth_keys')
hiddenimports += collect_submodules('cryptography')
hiddenimports += collect_submodules('httpx')
hiddenimports += collect_submodules('eth_account')
hiddenimports += collect_submodules('eth_keys')
hiddenimports += collect_submodules('eth_utils')
hiddenimports += collect_submodules('eth_abi')
hiddenimports += collect_submodules('eth_hash')
hiddenimports += collect_submodules('eth_rlp')
hiddenimports += collect_submodules('hexbytes')
hiddenimports += collect_submodules('rlp')
hiddenimports += collect_submodules('cytoolz')
hiddenimports += collect_submodules('toolz')
hiddenimports += collect_submodules('parsimonious')
hiddenimports += collect_submodules('bitarray')
hiddenimports += collect_submodules('websockets')
hiddenimports += ['polymarket_us_adapter']
hiddenimports += collect_submodules('polymarket_us')
hiddenimports += collect_submodules('nacl')
hiddenimports += ['certifi']
datas += collect_data_files('certifi')

binaries = []
for _pkg in ('py_clob_client_v2', 'poly_eip712_structs', 'py_order_utils', 'Crypto'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

for _pkg in ('anthropic', 'openai', 'httpx2', 'jiter', 'distro'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

for _pkg in ('keyring', 'secretstorage', 'jeepney'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass
try:
    datas += copy_metadata('keyring')
except Exception:
    pass

for _pkg in ('py_ecc', 'eth_keyfile', 'eth_abi', 'eth_rlp', 'eth_typing',
             'eth_hash', 'hexbytes', 'rlp', 'bitarray', 'ckzg', 'toolz',
             'cytoolz', 'pycryptodome', 'poly_eip712_structs', 'py_order_utils',
             'py_clob_client_v2', 'pydantic', 'pydantic_core', 'parsimonious',
             'regex'):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass
for _pkg in ('py_ecc', 'eth_keyfile'):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass


a = Analysis(
    ['service.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='krypt-polybot-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='krypt-polybot-backend',
)
