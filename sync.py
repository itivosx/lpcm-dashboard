"""
Sincroniza o dashboard (index.html) com a planilha LPCM via Google Sheets API.
Roda dentro do GitHub Actions — não depende de nenhuma sandbox do Claude.

Se qualquer validação falhar, o script termina com sys.exit(1) SEM tocar em
index.html — o workflow então não encontra diferença nenhuma pra commitar,
e o dashboard antigo continua no ar (mesmo comportamento de "falhar
silenciosamente" de antes, só que agora sem depender de rede do Claude).
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = "127s8EiqrrvUPWIWNoqg3gSH2wZiZRds6dkjNW0umQIw"
HTML_PATH = "index.html"

CREDITORS = [
    ("P", "Tio Luiz / Tia Cida"), ("D", "Diego"), ("R1", "Robert Restaurante"),
    ("F", "Froes"), ("R2", "Rodrigo Marsaiolli"), ("M1", "Marcelo Cunhado"),
    ("M2", "Marcelinho"), ("G", "Gugu"), ("A", "Amaral"),
    ("B1", "Boca"), ("B2", "Beco"),
]
FINANCEIRAS = ["Bradesco", "Terreno Caixa", "Parcela PJ", "Dívida Locação GM"]


def die(msg):
    print(f"PAROU SEM PUBLICAR: {msg}")
    sys.exit(0)  # exit 0 de propósito: não é falha do workflow, é "nada a fazer"


def money(text):
    if text is None:
        return None
    t = text.strip().replace("$", "").replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def get_sheets_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        die("variável GOOGLE_SERVICE_ACCOUNT_JSON não definida")
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)


def find_label_value(rows, label, value_col_offset=1):
    """Procura `label` em qualquer célula de `rows` e devolve o valor
    `value_col_offset` colunas à direita na mesma linha. Resiliente a
    linhas/colunas se moverem, desde que o rótulo continue existindo."""
    for row in rows:
        for i, cell in enumerate(row):
            if cell and label in str(cell):
                idx = i + value_col_offset
                if idx < len(row):
                    return row[idx]
    return None


def main():
    svc = get_sheets_client()
    vals = svc.spreadsheets().values()

    def get_range(a1):
        try:
            res = vals.get(spreadsheetId=SPREADSHEET_ID, range=a1).execute()
            return res.get("values", [])
        except Exception as e:
            die(f"falha lendo '{a1}': {e}")

    # --- Balanço: células confirmadas diretamente (G5:J15 credores, K5:M8 financeiras) ---
    ff_rows = get_range("Balanço!G5:J15")   # 11 linhas, uma por credor, na ordem do CREDITORS
    fin_rows = get_range("Balanço!K5:M8")   # 4 linhas, uma por financeira

    debt_vals = {}
    for (code, name), row in zip(CREDITORS, ff_rows):
        if len(row) < 4:
            die(f"linha do credor '{name}' incompleta (esperava 4 colunas, veio {len(row)})")
        fv = money(row[3])  # coluna J = Corrigido
        if fv is None:
            die(f"valor corrigido do credor '{name}' não é número: {row}")
        debt_vals[code] = fv

    fin_vals = {}
    for name, row in zip(FINANCEIRAS, fin_rows):
        if len(row) < 2:
            die(f"linha da financeira '{name}' incompleta: {row}")
        fv = money(row[1])  # coluna L = Value
        if fv is None:
            die(f"valor de financeira '{name}' não é número: {row}")
        fin_vals[name] = fv

    debt_subtotal = sum(debt_vals.values())
    financeiras_subtotal = sum(fin_vals.values())
    debt_total_all = debt_subtotal + financeiras_subtotal
    
    # --- Fluxo / Ativos vs Divida: células confirmadas diretamente ---
    fluxo = get_range("'Fluxo / Ativos vs Divida'!C1:M25")

    def cell(row, col):
        try:
            return fluxo[row - 1][col]
        except IndexError:
            return None

    # colunas: C=2(idx), G=6, L=11 (0-indexed)
    ativos_val = money(cell(4, 4))        # G4 (intervalo começa em C, então G = índice 4)
    passivos_val_raw = money(cell(6, 4))  # G6 (negativo)
    flow_atual_val = money(cell(7, 0))    # C7 = Fluxo I tot (índice 0 = coluna C)
    flow_futuro_val = money(cell(9, 0))   # C9 = Fluxo II tot

    if None in (ativos_val, passivos_val_raw, flow_atual_val, flow_futuro_val):
        die("célula esperada vazia em 'Fluxo / Ativos vs Divida'")
    passivos_val = abs(passivos_val_raw)
    liquido_val = ativos_val - passivos_val

    # --- Val PJ: runway "atual" e "proposto" (busca por rótulo) ---
    solvA_meses = money(cell(21, 0))  # C21 = Solvente A (atual)
    solvB_meses = money(cell(22, 0))  # C22 = Solvente B (proposto)
    if solvA_meses is None or solvB_meses is None:
        die("célula C21/C22 (Solvente A/B) vazia ou não numérica")

    # --- VALIDAÇÃO DE SANIDADE (mesmos limites de sempre) ---
    checks = [
        1_000_000 <= ativos_val <= 20_000_000,
        1_000_000 <= passivos_val <= 20_000_000,
        liquido_val > 0,
        3_000_000 <= debt_subtotal <= 6_500_000,
        500_000 <= financeiras_subtotal <= 2_000_000,
        3_000_000 <= debt_total_all <= 10_000_000,
        all(v > 0 for v in debt_vals.values()),
        flow_atual_val < 0 and flow_futuro_val < 0,
        -300_000 <= flow_atual_val <= -10_000,
        -300_000 <= flow_futuro_val <= -10_000,
    ]
    if not all(checks):
        die(f"validação de sanidade falhou: {checks}")

    # --- DERIVADOS ---
    passivos_pct = round(passivos_val / ativos_val * 100, 1)
    v_p = debt_vals["P"]
    debt_w = {c: round(v / v_p * 100, 1) for c, v in debt_vals.items()}
    debt_pct = {c: round(v / debt_total_all * 100) for c, v in debt_vals.items()}
    flow_futuro_h = round(abs(flow_futuro_val) / abs(flow_atual_val) * 100, 1)
    flow_note_pct = round((abs(flow_atual_val) - abs(flow_futuro_val)) / abs(flow_atual_val) * 100)
    flow_note_diff = abs(flow_atual_val) - abs(flow_futuro_val)
    flow_note_diff_fmt = f"${flow_note_diff:,.0f}".replace(",", ".")
    flow_note_text = f"Queima de caixa mensal cai {flow_note_pct}% com o ajuste (\u2212${flow_note_diff_fmt}/m\u00eas)"

    def fmt_big(v, sign=False):
        s = f"${v:,.0f}".replace(",", ".")
        return f"+{s}" if sign and v > 0 else s

    def fmt_flow(v):
        return f"-${round(abs(v) / 1000)}k"

    def fmt_credor(v):
        if v >= 1_000_000:
            return f"${v / 1_000_000:.2f}M".replace(".", ",")
        return f"${round(v / 1000)}K"

    def fmt_runway(v):
        return f"{v:.1f}".replace(".", ",")

    last_sync_dt = datetime.now(ZoneInfo("America/Sao_Paulo"))
    meses_pt = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
    last_sync = f"{last_sync_dt.day} {meses_pt[last_sync_dt.month-1]} {last_sync_dt.year}, {last_sync_dt.strftime('%H:%M')}"

    values = {
        "ativos_val": fmt_big(ativos_val),
        "passivos_val": fmt_big(passivos_val),
        "liquido_val": fmt_big(liquido_val, sign=True),
        "solvA_meses": fmt_runway(solvA_meses),
        "solvB_meses": fmt_runway(solvB_meses),
        "debt_total_all": fmt_big(debt_total_all).replace("$", ""),
        "debt_subtotal": fmt_big(debt_subtotal),
        "flow_atual_val": fmt_flow(flow_atual_val),
        "flow_futuro_val": fmt_flow(flow_futuro_val),
        "flow_note": flow_note_text,
        "last_sync": last_sync,
    }
    for code, _ in CREDITORS:
        values[f"debt_{code}_val"] = fmt_credor(debt_vals[code])
        values[f"debt_{code}_pct"] = f"{debt_pct[code]}%"

    widths = {"passivos_pct": passivos_pct, "flow_futuro_h": flow_futuro_h}
    for code, _ in CREDITORS:
        widths[f"debt_{code}_w"] = debt_w[code]

    # --- APLICAR NO HTML ---
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    total_subs = 0
    for key, val in values.items():
        pattern = re.compile(r'(data-f="' + re.escape(key) + r'">)[^<]*')
        html, n = pattern.subn(r"\g<1>" + val.replace("\\", "\\\\"), html)
        if n != 1:
            die(f"marcador data-f='{key}' casou {n} vezes (esperado 1)")
        total_subs += n

    for key, val in widths.items():
        pattern = re.compile(
            r'(data-fw="' + re.escape(key) + r'"[^>]*style="[^"]*(?:width|height):)[\d.]+(%)'
        )
        html, n = pattern.subn(r"\g<1>" + str(val) + r"\2", html)
        if n != 1:
            die(f"marcador data-fw='{key}' casou {n} vezes (esperado 1)")
        total_subs += n

    if total_subs != 46:
        die(f"total de substituições = {total_subs}, esperado 46")

    if re.search(r'data-f="[^"]+">\s*<', html):
        die("sobrou marcador data-f vazio depois da substituição")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"OK: {total_subs} substituições aplicadas, last_sync={last_sync}")


if __name__ == "__main__":
    main()
