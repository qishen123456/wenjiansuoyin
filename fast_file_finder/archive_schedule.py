"""Calendar-based reporting deadlines, evaluated in Asia/Shanghai."""
from __future__ import annotations
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Literal
from pydantic import BaseModel, Field, model_validator

ZONE = ZoneInfo('Asia/Shanghai')
CALENDAR_SOURCE = 'https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html'
# State Council General Office, 国办发明电〔2025〕7号. Includes make-up workdays.
HOLIDAY_SPANS = [('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),
 ('2026-04-04','2026-04-06'),('2026-05-01','2026-05-05'),('2026-06-19','2026-06-21'),
 ('2026-09-25','2026-09-27'),('2026-10-01','2026-10-07')]
HOLIDAYS = set()
for first,last in HOLIDAY_SPANS:
    d = date.fromisoformat(first)
    while d <= date.fromisoformat(last):
        HOLIDAYS.add(d); d += timedelta(days=1)
WORKDAYS = {date.fromisoformat(s) for s in ['2026-01-04','2026-02-14','2026-02-28','2026-05-09','2026-09-20','2026-10-10']}

class Schedule(BaseModel):
    mode: Literal['rolling','calendar'] = 'rolling'
    due_period: Literal['same','next'] = 'next'
    due_day: int = Field(default=1, ge=1, le=31)
    day_basis: Literal['calendar','workday'] = 'calendar'
    grace_days: int = Field(default=0, ge=0, le=60)
    holiday_policy: Literal['keep','next_workday','skip'] = 'keep'
    calendar: Literal['cn','weekend'] = 'cn'
    extra_holidays: list[date] = Field(default_factory=list, max_length=366)
    extra_workdays: list[date] = Field(default_factory=list, max_length=366)

    @model_validator(mode='after')
    def no_conflicting_dates(self):
        if set(self.extra_holidays) & set(self.extra_workdays):
            raise ValueError('同一日期不能同时设为休息日和工作日')
        return self

class CalendarUnavailable(ValueError):
    pass

def workday(d: date, s: Schedule) -> bool:
    if d in s.extra_workdays: return True
    if d in s.extra_holidays: return False
    if s.calendar == 'cn':
        if d.year != 2026: raise CalendarUnavailable(f'{d.year} 年法定节假日与调休日历尚未维护；请补充日历或明确改用双休日历')
        if d in WORKDAYS: return True
        if d in HOLIDAYS: return False
    return d.weekday() < 5

def month_add(d: date, n: int) -> date:
    index = d.year * 12 + d.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)

def period_start(d: date, kind: str) -> date:
    if kind == 'weekly': return d - timedelta(days=d.weekday())
    if kind == 'monthly': return d.replace(day=1)
    return d

def next_period(d: date, kind: str, offset=1) -> date:
    return month_add(d, offset) if kind == 'monthly' else d + timedelta(days=(7 if kind == 'weekly' else 1)*offset)

def advance(d: date, n: int, s: Schedule, inclusive=False) -> date:
    if s.day_basis == 'calendar': return d + timedelta(days=n - (1 if inclusive else 0))
    left = n
    if not inclusive: d += timedelta(days=1)
    for _ in range(800):
        if workday(d,s): left -= 1
        if left <= 0: return d
        d += timedelta(days=1)
    raise ValueError('工作日设置过密，无法计算截止日期')

def occurrence(start: date, kind: str, cutoff: str, s: Schedule) -> dict:
    end = next_period(start,kind)
    anchor = end if s.due_period == 'next' else start
    if s.day_basis == 'calendar' and kind == 'monthly':
        # A "31st" deadline in a short month means that month's final day.
        due = min(anchor + timedelta(days=s.due_day-1), month_add(anchor,1)-timedelta(days=1))
    else: due = advance(anchor,s.due_day,s,inclusive=True)
    if s.grace_days: due = advance(due,s.grace_days,s)
    original = due
    skipped = False
    if s.holiday_policy != 'keep' and not workday(due,s):
        if s.holiday_policy == 'skip': skipped = True
        else:
            for _ in range(800):
                due += timedelta(days=1)
                if workday(due,s): break
            else: raise ValueError('无法找到下一个工作日')
    dt = datetime.combine(due,time.fromisoformat(cutoff),ZONE)
    return {'period_start':str(start),'period_end':str(end-timedelta(days=1)),
            'window_start':datetime.combine(anchor,time.min,ZONE).timestamp(),
            'deadline':dt.timestamp(),'deadline_display':dt.strftime('%Y-%m-%d %H:%M'),
            'original_date':str(original),'adjusted':due!=original,'skipped':skipped}

def describe(kind: str, cutoff: str, s: Schedule) -> str:
    if s.mode != 'calendar': return '按距上次有效更新的间隔检测'
    label = {'daily':('当天','次日'),'weekly':('本周','下周'),'monthly':('本月','下月')}[kind][s.due_period=='next']
    unit = '工作日' if s.day_basis=='workday' else '天'
    day_label = f'第 {s.due_day} 个工作日' if s.day_basis=='workday' else f'第 {s.due_day} 天'
    if kind=='daily' and s.due_day==1 and s.day_basis=='calendar': day_label=''
    text = f'{label}{day_label} {cutoff} 前'
    if s.grace_days: text += f'，另宽限 {s.grace_days} 个{unit}'
    text += {'keep':'，休息日照常检测','next_workday':'，遇休息日顺延至下一工作日','skip':'，休息日跳过该期'}[s.holiday_policy]
    return text

def timeline(kind: str, cutoff: str, s: Schedule, now: datetime | None=None) -> dict:
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    if kind not in {'daily','weekly','monthly'}: raise ValueError('自定义间隔请使用滚动检测')
    current = period_start(now.date(),kind)
    # Enough lookback for maximum grace and holiday adjustment, without unbounded loops.
    behind = {'daily':150,'weekly':24,'monthly':6}[kind]
    ahead = {'daily':30,'weekly':8,'monthly':5}[kind]
    occurrences, warnings = [], set()
    for offset in range(-behind,ahead):
        start = next_period(current,kind,offset)
        try: occurrences.append(occurrence(start,kind,cutoff,s))
        except CalendarUnavailable as exc:
            if offset >= 0 or now.year != 2026: warnings.add(str(exc))
    occurrences.sort(key=lambda row:(row['deadline'],row['period_start']))
    past = [r for r in occurrences if not r['skipped'] and r['deadline'] <= now.timestamp()]
    upcoming = [r for r in occurrences if r['deadline'] > now.timestamp()][:3]
    missing_current = None
    if s.calendar=='cn' and (s.day_basis=='workday' or s.holiday_policy!='keep') and now.year!=2026:
        missing_current = f'{now.year} 年法定节假日日历尚未维护，已暂停此规则检测'
    return {'latest_due':past[-1] if past else None,'upcoming':upcoming,
            'summary':describe(kind,cutoff,s),'warnings':sorted(warnings),
            'calendar_error':missing_current,
            'calendar_source':CALENDAR_SOURCE if s.calendar=='cn' else '',
            'calendar_years':[2026] if s.calendar=='cn' else []}
