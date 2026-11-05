"""Bounded table previews. Never returns workbook bytes or evaluates formulas."""
import csv
from datetime import date, datetime
import itertools
import math
from pathlib import Path
import zipfile

MAX_ROWS=10000
MAX_COLUMNS=100
MAX_CELL_CHARS=2000


def cell(value):
    if value is None:return ''
    if isinstance(value,(datetime,date)):return value.isoformat(sep=' ') if isinstance(value,datetime) else value.isoformat()
    if isinstance(value,float) and not math.isfinite(value):return ''
    return str(value)[:MAX_CELL_CHARS]


def read(path, sheet, start, page_size):
    ext=path.suffix.lower()
    if ext in {'.xlsx','.xlsm','.xlsb','.ods'}:
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist())>128*1024*1024:
                raise ValueError('工作簿解压后超过 128 MB 预览上限，请拆分文件')
    sheets=[];rows=[];count=0;column_count=0;columns_truncated=False;cells_truncated=False
    def consume(iterator):
        nonlocal count,column_count,columns_truncated,cells_truncated
        for index,row in enumerate(itertools.islice(iterator,MAX_ROWS+1)):
            count=index+1
            column_count=max(column_count,min(len(row),MAX_COLUMNS))
            columns_truncated |= len(row)>MAX_COLUMNS
            if start<=index<start+page_size:
                cells_truncated |= any(len(str(v))>MAX_CELL_CHARS for v in row[:MAX_COLUMNS] if v is not None)
                rows.append([cell(v) for v in row[:MAX_COLUMNS]])
    if ext in {'.csv','.tsv'}:
        sheets=['数据']
        if sheet and sheet!='数据':raise ValueError('工作表不存在')
        for encoding in ['utf-8-sig','gb18030']:
            try:
                rows.clear();count=column_count=0
                with path.open(encoding=encoding,newline='') as source:
                    sample=source.read(8192);source.seek(0)
                    if ext=='.tsv':delimiter='\t'
                    else:
                        try:delimiter=csv.Sniffer().sniff(sample,delimiters=',;\t|').delimiter
                        except csv.Error:delimiter=','
                    consume(csv.reader(source,delimiter=delimiter))
                break
            except UnicodeDecodeError:
                if encoding=='gb18030':raise ValueError('文件编码无法识别')
    elif ext in {'.xlsx','.xlsm'}:
        import openpyxl
        workbook=openpyxl.load_workbook(path,read_only=True,data_only=True,keep_links=False)
        try:
            sheets=workbook.sheetnames[:50]
            chosen=sheet or (sheets[0] if sheets else '')
            if chosen not in sheets:raise ValueError('工作表不存在或超过前 50 个工作表的预览范围')
            ws=workbook[chosen]
            columns_truncated=(ws.max_column or 0)>MAX_COLUMNS
            consume(ws.iter_rows(max_row=MAX_ROWS+1,max_col=min(ws.max_column or MAX_COLUMNS,MAX_COLUMNS),values_only=True))
        finally:workbook.close()
    else:
        # Legacy engines are guarded by a smaller source-size cap at the API boundary.
        import pandas as pd
        with pd.ExcelFile(path) as workbook:sheets=workbook.sheet_names[:50]
        chosen=sheet or (sheets[0] if sheets else '')
        if chosen not in sheets:raise ValueError('工作表不存在')
        frame=pd.read_excel(path,sheet_name=chosen,header=None,nrows=MAX_ROWS+1,usecols=lambda index:index<MAX_COLUMNS)
        consume(frame.itertuples(index=False,name=None))
    return {'sheets':sheets,'sheet':sheet or sheets[0],'rows':rows,'row_count':min(count,MAX_ROWS),
            'column_count':column_count,'has_more':start+page_size<min(count,MAX_ROWS),
            'truncated':count>MAX_ROWS or columns_truncated or cells_truncated,'rows_truncated':count>MAX_ROWS,'columns_truncated':columns_truncated,'cells_truncated':cells_truncated,'row_limit':MAX_ROWS,'column_limit':MAX_COLUMNS,
            'cell_char_limit':MAX_CELL_CHARS,'start_row':start+1}
