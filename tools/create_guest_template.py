"""Genera il modello Excel standard per importare gli invitati."""
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.table import Table, TableStyleInfo

OUTPUT = Path("modello_invitati_event_checkin.xlsx")

def main():
    wb = Workbook()
    ws = wb.active
    ws.title = "Invitati"
    headers = ["Nome", "Cognome", "Codice identificativo", "Invitato da", "Numero tavolo", "Note"]
    ws.append(headers)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:F2"
    widths = [22, 22, 28, 28, 18, 40]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + idx)].width = width
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="244062")
        cell.alignment = Alignment(horizontal="center")
    ws.append(["Mario", "Rossi", "ABC12345", "Luca Bianchi", "12", "Esempio: eliminare questa riga"])
    table = Table(displayName="ListaInvitati", ref="A1:F2")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(table)

    info = wb.create_sheet("Istruzioni")
    rows = [
        ["MODELLO IMPORTAZIONE INVITATI - EVENT CHECK-IN"],
        ["1. Inserire un invitato per ogni riga nel foglio 'Invitati'."],
        ["2. Le colonne e le intestazioni devono rimanere invariate."],
        ["3. Nome, Cognome e Codice identificativo sono obbligatori."],
        ["4. Il Codice identificativo deve essere univoco all'interno dell'evento."],
        ["5. Invitato da, Numero tavolo e Note sono facoltativi."],
        ["6. Eliminare la riga di esempio prima di caricare il file."],
        ["7. Non inserire orari: ingresso e uscita vengono registrati dalla piattaforma."],
        ["8. L'importazione mostrerà un'anteprima e segnalerà eventuali duplicati."],
    ]
    for row in rows:
        info.append(row)
    info.column_dimensions["A"].width = 110
    info["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    info["A1"].fill = PatternFill("solid", fgColor="244062")
    for row in info.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
        info.row_dimensions[row[0].row].height = 25
    wb.save(OUTPUT)
    print(f"Creato: {OUTPUT.resolve()}")

if __name__ == "__main__":
    main()
