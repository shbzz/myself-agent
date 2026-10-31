"""Versioned synthetic two-module cases with host-owned acceptance, not live data."""

import base64
import copy
import textwrap

from myself_agent.recovery_store import digest


def command(source):
    encoded = base64.b64encode(textwrap.dedent(source).strip().encode()).decode()
    return f"python -I -B -c \"import base64; exec(base64.b64decode('{encoded}'))\""


ORDER_SPEC = """实现两个独立模块，不安装依赖、不联网，仅修改 parser.py 与 stats.py。
parser.py 的 parse_orders(csv_text) 接受CSV字符串，返回保持行顺序的字典列表。
列名去首尾空白且首列允许UTF-8 BOM；必需order_id、amount；可选currency，整列缺省时默认CNY。
order_id去首尾空白、非空且全表唯一；amount按Decimal解析，必须有限、非负且精确到整数分，
返回amount_cents整数，不能经float计算。1.230允许、1.231拒绝、1e1允许。
currency去空白转大写，仅支持CNY/USD/EUR；列存在但值为空也拒绝。
每条返回恰好order_id、amount_cents、currency三字段。无数据行返回空列表。
缺必需列、重复/空ID、无效金额/币种均抛ValueError。
stats.py 的 summarize(orders) 接受上述规范化列表，返回
{"count":记录数,"totals":按币种累计整数分的字典}；空列表为{"count":0,"totals":{}}。
不得混合币种相加或修改输入列表。两个模块必须可以独立调用和组合调用。
"""

EVENT_SPEC = """实现两个独立模块，不安装依赖、不联网，仅修改 normalize.py 与 report.py。
normalize.py 的 normalize_events(records) 接受字典列表，每条必须有user、time、tags。
user为字符串，去首尾空白后非空并保持大小写/Unicode；time为ISO8601字符串，必须有时区，
支持Z和±HH:MM偏移，转换为UTC后取YYYY-MM-DD日期（不可直接截原字符串前10位）。
tags是字符串列表，逐项strip/lower，去重、删除空字符串、升序排序。
返回保持输入顺序的字典列表，每条恰为user、date、tags三字段，不修改输入。
字段缺失、非法类型、空user、无效时间/无时区都抛ValueError。空列表返回空列表。
report.py 的 summarize_events(events) 接受上述规范化列表，按date升序返回字典列表；
每条为{"date":日期,"count":事件数,"unique_users":不同user数,"tag_counts":各标签出现次数字典}。
标签按每条事件中的规范化标签计算；用户大小写敏感。空列表返回空列表，不修改输入。
两个模块必须可独立调用和组合调用。
"""

ORDERS = {
    "id": "orders-v1",
    "spec": ORDER_SPEC,
    "files": {
        "parser.py": "def parse_orders(csv_text):\n    raise NotImplementedError\n",
        "stats.py": "def summarize(orders):\n    raise NotImplementedError\n",
    },
    "modules": [
        {
            "id": "parser",
            "file": "parser.py",
            "task": "按完整需求只实现parser.py的parse_orders，保留函数接口。",
            "check": command(r"""
import runpy
f=runpy.run_path('/workspace/parser.py')['parse_orders']
assert f('\ufeff order_id , amount , currency \n a ,1.23, cny \n b ,1e1,usd\n') == [
    {'order_id':'a','amount_cents':123,'currency':'CNY'},
    {'order_id':'b','amount_cents':1000,'currency':'USD'}]
assert f('order_id,amount\nx,1.230\ny,0\n') == [
    {'order_id':'x','amount_cents':123,'currency':'CNY'},
    {'order_id':'y','amount_cents':0,'currency':'CNY'}]
assert f('order_id,amount\n') == []
for bad in ['id,amount\na,1', 'order_id,amount\n,1', 'order_id,amount\na,1\na,2',
            'order_id,amount\na,-1', 'order_id,amount\na,NaN', 'order_id,amount\na,Infinity',
            'order_id,amount\na,1.231', 'order_id,amount,currency\na,1,',
            'order_id,amount,currency\na,1,XXX']:
    try: f(bad)
    except ValueError: pass
    else: raise AssertionError('invalid-order-not-rejected')
print('orders-parser-pass')
"""),
        },
        {
            "id": "stats",
            "file": "stats.py",
            "task": "按完整需求只实现stats.py的summarize，保留函数接口。",
            "check": command(r"""
import copy,runpy
f=runpy.run_path('/workspace/stats.py')['summarize']
data=[{'order_id':'a','amount_cents':123,'currency':'CNY'},
      {'order_id':'b','amount_cents':77,'currency':'CNY'},
      {'order_id':'c','amount_cents':999,'currency':'USD'}]
before=copy.deepcopy(data)
assert f(data)=={'count':3,'totals':{'CNY':200,'USD':999}}
assert data==before
assert f([])=={'count':0,'totals':{}}
print('orders-stats-pass')
"""),
        },
    ],
    "integration": command(r"""
import runpy
parse=runpy.run_path('/workspace/parser.py')['parse_orders']
summarize=runpy.run_path('/workspace/stats.py')['summarize']
assert summarize(parse('order_id,amount,currency\na,0.29,EUR\nb,0.01,EUR\nc,2,USD')) == {
    'count':3,'totals':{'EUR':30,'USD':200}}
print('orders-integration-pass')
"""),
}

EVENTS = {
    "id": "events-v1",
    "spec": EVENT_SPEC,
    "files": {
        "normalize.py": "def normalize_events(records):\n    raise NotImplementedError\n",
        "report.py": "def summarize_events(events):\n    raise NotImplementedError\n",
    },
    "modules": [
        {
            "id": "normalize",
            "file": "normalize.py",
            "task": "按完整需求只实现normalize.py的normalize_events。",
            "check": command(r"""
import copy,runpy
f=runpy.run_path('/workspace/normalize.py')['normalize_events']
data=[{'user':' Alice ','time':'2026-01-01T00:30:00+02:00','tags':[' AI ','ai','',' 数据 ']},
      {'user':'数据','time':'2025-12-31T23:30:00-02:00','tags':[]}]
before=copy.deepcopy(data)
assert f(data)==[{'user':'Alice','date':'2025-12-31','tags':['ai','数据']},
                 {'user':'数据','date':'2026-01-01','tags':[]}]
assert data==before and f([])==[]
assert f([{'user':'a','time':'2026-03-01T00:00:00Z','tags':['B','a']}])[0]['tags']==['a','b']
base={'user':'a','time':'2026-01-01T00:00:00Z','tags':[]}
for bad in [{},base|{'user':''},base|{'user':3},base|{'time':'not-a-time'},
            base|{'time':'2026-01-01T00:00:00'},base|{'tags':'ai'},base|{'tags':[1]}]:
    try: f([bad])
    except ValueError: pass
    else: raise AssertionError('invalid-event-not-rejected')
print('events-normalize-pass')
"""),
        },
        {
            "id": "report",
            "file": "report.py",
            "task": "按完整需求只实现report.py的summarize_events。",
            "check": command(r"""
import copy,runpy
f=runpy.run_path('/workspace/report.py')['summarize_events']
data=[{'user':'a','date':'2026-01-02','tags':['ai']},
      {'user':'b','date':'2026-01-01','tags':['数据']},
      {'user':'a','date':'2026-01-02','tags':['ai','数据']},
      {'user':'A','date':'2026-01-02','tags':[]}]
before=copy.deepcopy(data)
assert f(data)==[{'date':'2026-01-01','count':1,'unique_users':1,'tag_counts':{'数据':1}},
                {'date':'2026-01-02','count':3,'unique_users':2,'tag_counts':{'ai':2,'数据':1}}]
assert data==before and f([])==[]
print('events-report-pass')
"""),
        },
    ],
    "integration": command(r"""
import runpy
normalize=runpy.run_path('/workspace/normalize.py')['normalize_events']
report=runpy.run_path('/workspace/report.py')['summarize_events']
sample=[{'user':' u ','time':'2026-01-01T01:00:00+02:00','tags':['AI','ai']}]
assert report(normalize(sample)) == [
    {'date':'2025-12-31','count':1,'unique_users':1,'tag_counts':{'ai':1}}]
print('events-integration-pass')
"""),
}


def cases():
    result = copy.deepcopy([ORDERS, EVENTS])
    for case in result:
        case["checks"] = [module["check"] for module in case["modules"]] + [case["integration"]]
        prefix = case["id"].split("-v")[0]
        case["markers"] = [prefix + "-" + module["id"] + "-pass" for module in case["modules"]]
        case["markers"].append(prefix + "-integration-pass")
        case["case_sha256"] = digest(case)
    return result
