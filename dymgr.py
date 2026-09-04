import json, time, datetime, difflib, httpx, re, io, base64
import asyncio
import configparser as cfg
import os
from os.path import dirname, join, exists, getmtime
from PIL import Image
from .res import drawCard
from .res import wbi
from .res import auth
from .res.getImg import get_Image
from loguru import logger as log
from .res.httpx_compat import async_client

if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

help_info="""=== bili-notice-hoshino 帮助 ===
    
bili-ctl para1 para2 para3 [...]
关键词过滤  black-words  uid  add/remove 拼多多 pdd ... 
查看关键词  black-words  uid  list  
开奖动态   islucky  uid  true/false
重新加载    reload
昵称控制    add-nick/del-nick   uid  短昵称
昵称查询    list-lick   uid
帮助菜单   help
*功能性指令只能由机器人管理员操作*"""

# 路径配置
curpath = dirname(__file__)
watcher_file = join(curpath, 'upperlist.json')
res_dir = join(curpath,'res/')
up_dir = join(curpath,'uppers/')

# 全局变量
number = 0              # 轮询的编号
up_latest = {}          # 各个up主及其动态记录
live_latest = {}        # 各个up主直播状态记录
up_list=[]              # up主列表
cache_clean_date = 0
number_live = 0         # 直播轮询的编号
flag_number_live = 5     # 默认每轮询5次，检查是否有主播开播。
gcookies = None
cookies_fail = 0

p = {
    "all://":None
}

WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
DYNAMIC_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,onlyfansAssetsV2,forwardListHidden,ugcDelete,commentsNewVersion"


def bili_web_headers(referer="https://www.bilibili.com/"):
    return {
            "Accept":"application/json, text/plain, */*",
            "Accept-Encoding":"gzip, deflate, br",
            "Accept-Language":"zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Connection":"keep-alive",
            "Origin":"https://www.bilibili.com",
            "Referer":referer,
            "Sec-Ch-Ua":"\"Not/A)Brand\";v=\"8\", \"Chromium\";v=\"126\", \"Microsoft Edge\";v=\"126\"",
            "Sec-Ch-Ua-Mobile":"?0",
            "Sec-Ch-Ua-Platform":"\"Windows\"",
            "Sec-Fetch-Dest":"empty",
            "Sec-Fetch-Mode":"cors",
            "Sec-Fetch-Site":"same-site",
            "User-Agent":WEB_USER_AGENT
            }

def image_to_base64(img):
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return 'base64://' + base64.b64encode(bio.getvalue()).decode()


def rich_text_nodes_to_text(nodes):
    if not isinstance(nodes, list):
        return ""
    parts = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        word = node.get("word") or {}
        rich = node.get("rich") or {}
        value = word.get("words") or rich.get("text") or node.get("text") or ""
        parts.append(str(value))
    return "".join(parts)


def get_web_module(card: dict, key: str):
    modules = card.get("modules", {})
    if isinstance(modules, dict):
        return modules.get(key) or {}
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            value = module.get(key)
            if value:
                return value
    return {}


def get_web_dynamic_content(card: dict):
    content = get_web_module(card, "module_content")
    paragraphs = content.get("paragraphs", []) if isinstance(content, dict) else []
    text_parts = []
    image_items = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        text_info = paragraph.get("text") or {}
        text = rich_text_nodes_to_text(text_info.get("nodes", [])) if isinstance(text_info, dict) else ""
        if text:
            text_parts.append(text)
        pic_info = paragraph.get("pic") or {}
        if isinstance(pic_info, dict):
            image_items.extend(pic_info.get("pics") or [])

    # 兼容空间动态列表接口返回的简化结构。
    dynamic = get_web_module(card, "module_dynamic")
    major = dynamic.get("major") or {} if isinstance(dynamic, dict) else {}
    draw_info = major.get("draw") or {}
    opus_info = major.get("opus") or {}
    if isinstance(draw_info, dict):
        image_items.extend(draw_info.get("items") or [])
    if isinstance(opus_info, dict):
        image_items.extend(opus_info.get("pics") or [])

    return "\n".join(text_parts).strip(), image_items


def get_web_dynamic_title(card: dict, nick: str):
    title_info = get_web_module(card, "module_title")
    title = title_info.get("text", "") if isinstance(title_info, dict) else ""
    text, _ = get_web_dynamic_content(card)
    title = str(title or "").strip()
    default_title = f"{nick}的动态"
    if not title or title == default_title:
        excerpt = re.sub(r"\\s+", " ", text).strip()
        title = excerpt[:50] + ("..." if len(excerpt) > 50 else "")
    return title or "发布了一条动态"


async def draw_web_dynamic_card(card: dict, this_up: dict):
    author = get_web_module(card, "module_author")
    dynamic = get_web_module(card, "module_dynamic")
    major = dynamic.get("major") or {} if isinstance(dynamic, dict) else {}
    major_type = major.get("type", "") if isinstance(major, dict) else ""
    card_type = card.get("type", "")
    if card_type not in ["", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_FORWARD", "DYNAMIC_TYPE_WORD"] and major_type not in ["MAJOR_TYPE_DRAW", "MAJOR_TYPE_OPUS"]:
        return None, ""

    box = drawCard.Box(conf)
    face_url = author.get("face") if isinstance(author, dict) else ""
    face = await get_Image(Type="face", url=face_url) if face_url else Image.new('RGBA', (42,42), 'white')
    faceimg = box.face(face)
    nick = (author.get("name") if isinstance(author, dict) else "") or this_up.get("uname", "未知UP")
    pub_time, _ = get_card_pub_time(card)
    nickimg = box.nickname(nick=nick, time=time.strftime("%y-%m-%d %H:%M", time.localtime(pub_time)))

    text, items = get_web_dynamic_content(card)
    title = get_web_dynamic_title(card, nick)
    pics = []
    seen_urls = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("src") or item.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            pics.append(await get_Image(Type="image", url=url))
        except Exception as e:
            log.warning(f'新版动态图片下载失败：{e}')

    log.debug(f'新版动态卡片内容：id={card.get("id_str")}, title_len={len(title)}, text_len={len(text)}, pics={len(pics)}, major_type={major_type}')
    if pics:
        bodyimg = box.image(title, None, pics[:9], min(len(pics), 9))
        dytype = "图文动态"
    else:
        bodyimg = box.text(title)
        dytype = "动态"

    stat = get_web_module(card, "module_stat")
    stat = stat if isinstance(stat, dict) else {}
    bottomimg = box.bottom(
        (stat.get("forward") or {}).get("count", 0),
        (stat.get("comment") or {}).get("count", 0),
        (stat.get("like") or {}).get("count", 0)
    )
    img = box.combine(face=faceimg, nick=nickimg, body=bodyimg, bottom=bottomimg)
    return image_to_base64(img), dytype


def fallback_dynamic_info(card: dict, this_up: dict):
    author = get_web_module(card, "module_author")
    dynamic = get_web_module(card, "module_dynamic")
    major = dynamic.get("major") or {} if isinstance(dynamic, dict) else {}
    major_type = major.get("type", "") if isinstance(major, dict) else ""
    card_type = card.get("type", "")
    dy_type = "动态"
    if card_type == "DYNAMIC_TYPE_FORWARD":
        dy_type = "转发"
    elif major_type == "MAJOR_TYPE_ARCHIVE":
        dy_type = "视频"
    elif major_type == "MAJOR_TYPE_DRAW":
        dy_type = "图文动态"
    elif major_type == "MAJOR_TYPE_ARTICLE":
        dy_type = "专栏"
    elif major_type == "MAJOR_TYPE_OPUS":
        dy_type = "图文动态"
    elif major_type == "MAJOR_TYPE_LIVE_RCMD":
        dy_type = "直播动态"
    elif dynamic.get("desc"):
        dy_type = "文字动态"

    return {
        "nickname": author.get("name") or this_up.get("uname", "未知UP"),
        "uid": int(card["id_str"]),
        "type": dy_type,
        "subtype": major_type or card_type,
        "time": int(author.get("pub_ts", time.time())),
        "pic": "",
        "link": f'https://t.bilibili.com/{card["id_str"]}',
        "sublink": "",
        "group": this_up["group"]
    }


def parse_bili_pub_time(pub_time: str):
    if not pub_time:
        return None
    now = int(time.time())
    text = str(pub_time).strip()
    m = re.search(r'(\d+)\s*分钟前', text)
    if m:
        return now - int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*小时前', text)
    if m:
        return now - int(m.group(1)) * 3600
    m = re.search(r'(\d+)\s*天前', text)
    if m:
        return now - int(m.group(1)) * 86400
    if text.startswith('昨天'):
        return now - 86400
    if text.startswith('刚刚'):
        return now
    m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*(\d{1,2}:\d{1,2})?', text)
    if m:
        hour, minute = (0, 0)
        if m.group(4):
            hour, minute = [int(x) for x in m.group(4).split(':')]
        return int(datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hour, minute).timestamp())
    m = re.search(r'(\d{1,2})[-/](\d{1,2})\s*(\d{1,2}:\d{1,2})?', text)
    if m:
        hour, minute = (0, 0)
        if m.group(3):
            hour, minute = [int(x) for x in m.group(3).split(':')]
        year = datetime.datetime.now().year
        return int(datetime.datetime(year, int(m.group(1)), int(m.group(2)), hour, minute).timestamp())
    return None


def get_card_pub_time(card: dict):
    author = get_web_module(card, "module_author")
    pub_ts_raw = author.get("pub_ts") if isinstance(author, dict) else None
    pub_time_text = (author.get("pub_time") or author.get("pub_action") or "") if isinstance(author, dict) else ""
    parsed_text_time = parse_bili_pub_time(pub_time_text)

    try:
        pub_ts = int(pub_ts_raw)
    except Exception:
        pub_ts = None

    if parsed_text_time is not None:
        if pub_ts is None or abs(int(time.time()) - pub_ts) > 30 * 86400:
            return parsed_text_time, f'pub_time={pub_time_text}, pub_ts={pub_ts_raw}'

    if pub_ts is not None:
        return pub_ts, f'pub_ts={pub_ts_raw}, pub_time={pub_time_text}'

    return int(time.time()), f'no valid pub time, pub_ts={pub_ts_raw}, pub_time={pub_time_text}'


async def append_web_fallback(dynamic_list: list, card: dict, this_up: dict, header: dict, cookies) -> bool:
    web_card = await get_web_dynamic_detail(card["id_str"], header, cookies)
    source_card = web_card or card
    try:
        pic, dytype = await draw_web_dynamic_card(source_card, this_up)
    except Exception as e:
        log.warning(f'新版动态图片卡片生成失败：{e}')
        pic, dytype = None, ""

    dyinfo = fallback_dynamic_info(source_card, this_up)
    if pic:
        dyinfo["pic"] = pic
        dyinfo["type"] = dytype or dyinfo["type"]
        log.info(f'新版动态图片卡片生成成功：{card["id_str"]}')
    else:
        log.info(f'新版动态图片卡片不可用，使用纯链接推送：{card["id_str"]}')
    dynamic_list.append(dyinfo)
    return True


async def get_web_dynamic_detail(dynamic_id: str, header: dict, cookies):
    params = {
        "id": dynamic_id,
        "timezone_offset": -480,
        "platform": "web",
        "gaia_source": "main_web",
        "features": DYNAMIC_FEATURES,
        "web_location": "333.1368",
    }
    try:
        async with async_client(proxies=p) as client:
            res = await client.get(
                url='https://api.bilibili.com/x/polymer/web-dynamic/v1/detail',
                params=params,
                headers=header,
                cookies=cookies
            )
    except Exception as e:
        log.warning(f'新版动态详情接口请求失败：{e}')
        return None

    if res.status_code != 200:
        log.warning(f'新版动态详情接口返回HTTP={res.status_code}, body={res.text[:300]}')
        return None

    try:
        detail = res.json()
    except json.JSONDecodeError:
        log.warning(f'新版动态详情接口返回非JSON: body={res.text[:300]}')
        return None

    if detail.get("code") != 0:
        log.warning(f'新版动态详情接口返回code={detail.get("code")}, message={detail.get("message")}')
        return None

    return detail.get("data", {}).get("item")


def up_history_write(uid:str, skin=None):
    global up_latest, up_dir, live_latest

    if skin == None and exists(up_dir+uid+'.json'):
        with open(up_dir+uid+'.json','r', encoding='UTF-8') as f:
            j = json.load(f)
        if 'skin' in j:
            skin = j['skin']
    else:
        skin={}
    with open(up_dir+uid+'.json','w', encoding='UTF-8') as f:     # 更新记录文件
        json.dump({
                    "history": up_latest[uid],
                    "live": live_latest[uid],
                    "skin": skin
                    },
                   f, ensure_ascii=False)


    


# 读取配置文件
if not exists(join(curpath, 'config.ini')):
    try:
        os.rename(join(curpath, 'config_example.ini'),join(curpath, 'config.ini'))
    except:
        print("\r\n\033[1;41m[Error]\033[0m\tBili-notice:\tCannot Find config.ini or config_example.ini !!!")
conf = cfg.ConfigParser(allow_no_value=True)
conf.read(join(curpath, 'config.ini'), encoding='utf-8')
comcfg = conf.items('common')
drawcfg = conf.items('drawCard')

if conf.has_option('common','pool_live'):
    flag_number_live = int(conf.get('common','pool_live'))
if conf.getboolean('common','only_video'):
    available_type = [8]
elif conf.getboolean('common','only_dynamic'):
    available_type = [2,4]
else:
    available_type=[
        2,      # Picture
        4,      # text
        8,      # video
        64,     # article
        256,    # audio
        512,    # bangumi
        2048    # H5event
    ]

if conf.has_option('common','allow_follow_illegal'):
    allow_follow_illegal = conf.getboolean('common','allow_follow_illegal')
else:
    allow_follow_illegal = False

log_level = conf.get('common','log_level').upper()
if log_level not in ['ERROR', 'WARN', 'INFO', 'DEBUG', 'TRACE']:
    print(f'Config Error: log_level(={log_level}) not correct! Force log_level to INFO')
    log_level = 'INFO'
log_max_days = conf.get('common', 'log_max_days')
if not log_max_days.isdigit():
    log_max_days = 15
    print(f'Config Error: log_max_days get ({log_max_days}), we need number!')

# 初始化日志系统
path_log = join(dirname(__file__), "log/")
if not exists(path_log):
    os.mkdir(path_log)
log.add(
    path_log+'{time:YYYY-MM-DD}.log',
    level = log_level,
    rotation = "04:00",
    retention = log_max_days+" days",
    backtrace = False,              # 调试，生产请改为False
    enqueue = True,
    diagnose = False,              # 调试，生产请改为False
    format = '{time:HH:mm:ss} [{level}] \t{message}'
)


# 从文件中读取up主配置列表和up主发送动态的历史
up_group_info, up_list={}, []
if exists(up_dir + 'list.json'):
    with open(join(up_dir,'list.json'), 'r', encoding='UTF-8') as f:
        up_group_info = json.load(f)

    up_list = list(up_group_info.keys())
    for uid in up_list:
        if exists(up_dir+uid+'.json'):
            with open(up_dir+uid+'.json','r', encoding='UTF-8') as f:
                j = json.load(f)

            up_latest[uid] = j["history"]

            if 'live' in j:
                live_latest[uid] = j["live"]
            else:
                live_latest[uid] = 0
                up_history_write(uid)
                
        else:
            up_latest[uid]=[]
            live_latest[uid] = 0
            up_history_write(uid)
    
log.info(f'Load up list success: {len(up_list)}')

# 组成昵称查找
gw_user = {}
gw_nick = {}

for id in up_group_info:
    u = up_group_info[id]
    if u.get("nick"):
        gw_user[u["uname"]] = {"uid":u["uid"], "nick":u["nick"]}
        for n in u["nick"]:
            gw_nick[n] = {"uname":u["uname"], "uid":u["uid"]}
    else:
        gw_user[u["uname"]] = {"uid":u["uid"], "nick":[]}
gw_name_list = gw_user.keys()
gw_nick_list = gw_nick.keys()


async def get_update():
    global number,up_latest, up_list, cache_clean_date, up_group_info, number_live, flag_number_live, cookies_fail
    msg,dyimg,dytype = None,None,None
    rst, suc, fai=0,0,0
    dynamic_list=[]
    header=bili_web_headers()
    if number>=len(up_list):          # 序号异常大，清除
        number = 0

    if len(up_group_info) == 0:
        return 0, []

    # 借用轮询来清理垃圾和检查更新
    cache_clean_today = datetime.date.today().day
    if not cache_clean_today == cache_clean_date:
        clean_cache()
        await check_plugin_update()
        cache_clean_date = cache_clean_today
    # 尝试更新cookies
    gcookies = await auth.update_cookies(cookies_fail)
    cookies_fail = 0
    if gcookies == None:
        log.warning('get_update() 未获取到B站cookies，动态接口更容易触发412风控。')

    # 提取下一个up，如果没有人关注的话，状态改成false，跳过不关注的人
    maxcount = len(up_list)

    # 每flag_number_live次轮询，查一次直播间信息
    number_live += 1
    if flag_number_live <= number_live:
        print("Check live rooms.")
        number_live=0
        liverst,livelist=await live_check()
        # 由于发现了新的api可以一次性查询所有直播间，与之前的设计不同，直播功能无法很好的结合进原有代码中。
        # 所以，这里采用直接return的方法，避免产生其他纠葛
        return liverst, livelist
    
    while 1:
        if up_list[number] not in up_group_info.keys():
            if number+1>=len(up_list):          # 最多进行一轮
                number = 0
            else:
                number = number+1
            continue
        this_up = up_group_info[up_list[number]]
        if this_up["watch"] == True:                # 跳过不监控的up
            if len(this_up["group"]) == 0:          # 如果没有群关注up，就更改状态为不监控
                up_group_info[up_list[number]]["watch"]=False
                with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:
                    json.dump(up_group_info, f, ensure_ascii=False)
                continue            # 状态更新完成，下一个
            else:
                break               # up主状态正常，跳出循环
        else:
            if maxcount <= 0:       # 避免死循环不跳出
                return
            else:
                maxcount = maxcount -1
            if number+1>=len(up_list):          # 最多进行一轮
                number = 0
            else:
                number = number+1
    if this_up["watch"]:
        uid_str = up_list[number]
        print(f'Check list of {uid_str}')
        header = bili_web_headers(f'https://space.bilibili.com/{uid_str}/dynamic')
        # print(f'[Debug] Start getting ID={uid_str}')
        try:
            dynamic_params = {
                "host_mid": uid_str,
                "timezone_offset": -480,
                "platform": "web",
                "features": DYNAMIC_FEATURES,
                "web_location": "333.1365"
            }
            async with async_client(proxies=p) as client:
                res = await client.get(
                    url='https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space',
                    params=dynamic_params,
                    headers=header,
                    cookies=gcookies
                )
        except:
            log.info('Err: Get dynamic list failed.')
            number = 0 if number+1>=len(up_list) else number+1
            return -1, []
        if not res.status_code == 200:
            log.warning(f'get_update() fail: Server status code = {res.status_code}, body={res.text[:300]}')
            cookies_fail = 1
            number = 0 if number+1>=len(up_list) else number+1
            return -1, []

        res.encoding = 'utf-8'          # 兼容python3.9
        # print(f'收到的结果长度有{len(res.text)}, 其中 code=', end='' )
        try:
            dylist = res.json()
        except json.JSONDecodeError:
            log.warning(f'动态列表返回内容不是JSON: status={res.status_code}, body={res.text[:300]}')
            cookies_fail = 1
            number = 0 if number+1>=len(up_list) else number+1
            return -1, []

        if not dylist["code"] == 0:
            log.warning(f'dynamic list get fail: Server OK but code={dylist["code"]}, message={dylist.get("message")}, 您可能被风控. ')
            number = 0 if number+1>=len(up_list) else number+1
            return -1, []
        # 可能返回空内容
        
        items = dylist["data"]["items"]
        history_ids = set(int(x) for x in up_latest[up_list[number]])
        returned_ids = [int(card["id_str"]) for card in items]
        fresh_ids = [dyid for dyid in returned_ids if dyid not in history_ids]
        log.debug(f'获取到{uid_str}动态数量：{len(items)}，历史记录数量：{len(history_ids)}，未处理动态数量：{len(fresh_ids)}')
        log.debug(f'{uid_str}返回动态ID：{returned_ids[:20]}')
        if fresh_ids:
            log.debug(f'{uid_str}未处理动态ID：{fresh_ids[:20]}')
        else:
            log.debug(f'{uid_str}没有未处理动态，本轮不会推送')

        if len(items) == 0:
            log.warning(f'获取到的动态数量为0, 您可能被风控.')
            cookies_fail = 1
            number = 0 if number+1>=len(up_list) else number+1
            return -1, []
        
        for card in items:
            # api更改，动态卡片id变化。不要在遇到已处理动态时直接break，避免置顶/排序变化导致漏掉后面的新动态。
            if int(card["id_str"]) in up_latest[up_list[number]]:
                continue

            # 发布时间判断逻辑提前。新版接口有时pub_ts异常，优先用网页展示的pub_time兜底。
            pub_time, pub_time_source = get_card_pub_time(card)
            log.debug(f'dynamic({card["id_str"]}) type={card.get("type")} time_source={pub_time_source}')
            if conf.getint('common','available_time') *60 < (int(time.time()) - pub_time):
                log.info(f"This dynamic({card['id_str']}) is too old: {m2hm(time.time() - pub_time)} minutes ago, {pub_time_source}\n")
                up_latest[uid_str].append(int(card["id_str"]))       # (无论成功失败)完成后把动态加入肯德基豪华午餐
                up_history_write(uid_str)     # 更新记录文件
                fai -= 1
                continue

            # 获取动态信息，这个是旧版API格式，可以不用大幅修改解析代码 / 2024.07.14
            async with async_client(proxies=p) as client:
                res = await client.get(url=f'https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/get_dynamic_detail?dynamic_id={card["id_str"]}',headers=header, cookies=gcookies)
            if not res.status_code == 200:
                log.warning(f'Dynamic detail API return = {res.status_code}, 尝试新版动态详情接口')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue
            try:
                old_card = res.json()
            except json.JSONDecodeError:
                log.warning(f'动态详细信息返回内容不是JSON: status={res.status_code}, body={res.text[:300]}，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue

            if not old_card["code"] == 0:
                log.warning(f'dynamic detail info get fail: Server OK but code={old_card["code"]}, message={old_card.get("message")}，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue

            if len(res.text) < 100:
                log.warning(f'动态详细信息太短, 只有{len(res.text)}字节，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue

            # 解析动态json, 由于API变化需要重写
            try:
                dynamic = drawCard.Card(old_card["data"]["card"])
            except Exception as e:
                log.warning(f'动态内容解析异常：{e}，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue


            if not dynamic.json_decode_result:
                log.error(f'动态内容解析失败，id={card["id_str"]}，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                up_latest[uid_str].append(int(card["id_str"]))
                up_history_write(uid_str)
                suc += 1
                continue

            # 更新UP主的昵称
            if not dynamic.nickname == this_up["uname"]:
                log.info(f'更新UP主名称:  uid={this_up["uid"]}, nickname [{this_up["uname"]}] ==> [{dynamic.nickname}]')
                up_group_info[up_list[number]]["uname"] = dynamic.nickname
                with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:
                    json.dump(up_group_info, f, ensure_ascii=False)
            
            log.info('========== New Dynamic Card =========')
            log.info(f"UP={dynamic.nickname}({dynamic.uid}), Dynamic_id={dynamic.dyid}, Type={int(dynamic.dytype)}, ori_type={int(dynamic.dyorigtype)}")
            if (not conf.getboolean('common','repost')) and dynamic.dytype == 1:
                log.info(f"已设置不分享转发类动态。\n")
                fai -= 1
                continue
            try:
                if(dynamic.dytype == 64):
                    log.debug(f'动态类型64, 检查问题所在:')
                if not dynamic.check_black_words(conf.get('common','global_black_words'), this_up["ad_keys"], this_up["islucky"]):  # 如果触发过滤关键词，则忽视该动态
                    if(dynamic.dytype == 64):
                        log.debug(f'动态类型64, 通过了黑名单词汇检查')
                    if dynamic.is_realtime(conf.getint('common','available_time')):             # 太久的动态不予发送
                        # 只解析支持的类型
                        
                        if dynamic.dytype in available_type or (dynamic.dytype==1 and dynamic.dyorigtype in available_type):
                            drawBox = drawCard.Box(conf)       # 创建卡片图片的对象
                            dyimg, dytype = await dynamic.draw(drawBox, conf.getboolean('cache', 'dycard_cache'))   # 绘制动态
                            # msg = f"{dynamic.nickname} {dytype}, 点击链接直达：\n https://t.bilibili.com/{dynamic.dyidstr}  \n[CQ:image,file={dyimg}]"
                            dyinfo = {
                                "nickname": dynamic.nickname,
                                "uid":      dynamic.dyid,
                                "type":     dytype,
                                "subtype":  dynamic.dyorigtype,
                                "time":     dynamic.dytime,         # 时间戳，非字符串时间
                                "pic":      dyimg,
                                "link":     f'https://t.bilibili.com/{dynamic.dyidstr}',
                                "sublink":  "",
                                "group":    this_up["group"]
                            }
                            if(dynamic.dytype == 64):
                                log.debug(f'动态类型64, 绘制并组织完成')
                            
                            dynamic_list.append(dyinfo)
                            suc+=1
                        else:
                            log.info(f'(type={dynamic.dytype}, subtype={dynamic.dyorigtype}) 未受支持，使用列表接口信息进行纯链接推送\n')
                            await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                            suc += 1
                    else:
                        log.info(f"This dynamic({dynamic.dyid}) is too old: {m2hm(time.time() - dynamic.dytime)} minutes ago\n")
                        fai -=1
                else:
                    log.info(f"({dynamic.dyid})触发过滤词，或者是转发抽奖动态。\n")
                    fai -= 1 
            except Exception as e:
                if(dynamic.dytype == 64):
                    log.debug(f'动态类型64, 触发了try-exception')
                log.warning(f'动态图片渲染失败：{e}，使用列表接口信息进行纯链接推送')
                await append_web_fallback(dynamic_list, card, this_up, header, gcookies)
                suc += 1
            finally:
                up_latest[uid_str].append(dynamic.dyid)         # (无论成功失败)完成后把动态加入肯德基豪华午餐
                up_history_write(uid_str, dynamic.getskin())     # 更新记录文件
                log.debug(f'Write skin info into up history\n')
            
    rst = fai if suc==0 else suc
    number = 0 if number+1>=len(up_list) else number+1
    return rst, dynamic_list


async def follow(uid, group): # sync to async
    global number,up_latest, up_list, gcookies
    retry_time=3
    """关注UP主,并创建和修改对应的记录文件

    Args:
        uid (num): up主的uuid,仅接受通过uuid来关注
        gruop (num): 申请的群

    Returns:
        rst (bool): 申请的结果。
        msg (str):  结果的原因。成功后是  昵称[id]
    """
    if not uid.isdigit():
        msg = '请输入正确的UID!'
        log.info(f"关注失败,UID错误: {uid}")
        return False, msg

    header = {
                'Accept': 'application/json, text/plain, */*',
                'Accept-Encoding': 'gzip, deflate',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'Connection': 'close',
                'Host': 'api.bilibili.com',
                # 'Upgrade-Insecure-Requests': '1',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36 Edg/112.0.1722.58'}

    if uid not in up_list:  # 从未添加过
        para={"mid":str(uid)}  
        while True:
            # wbi认证尝试三次失败，放弃继续尝试并增加无名氏
            if retry_time <=0:
                msg='【联网核查失败】'
                return follow_illegal(uid, group, msg)
            # 从服务器获取信息
            try:
                if not gcookies == None:
                    async with async_client(proxies=p) as client:
                        res = await client.get(url=f'https://api.bilibili.com/x/space/wbi/acc/info', params=wbi.encode(para), headers=header, cookies=gcookies)
                    # res = requests.get(url=f'https://api.bilibili.com/x/space/wbi/acc/info', params=wbi.encode(para), headers=header, cookies=gcookies)
                else:
                    print("小饼干不存在")
                    async with async_client(proxies=p) as client:
                        res = await client.get(url=f'https://api.bilibili.com/x/space/wbi/acc/info', params=wbi.encode(para), headers=header, cookies=gcookies)
                    # res = requests.get(url=f'https://api.bilibili.com/x/space/wbi/acc/info', params=wbi.encode(para), headers=header, cookies=gcookies)
            except:
                msg="网络出错了，请稍后再试~"
                log.info('关注失败，网络错误')
                return False, msg
            # 应付阿b返回两段json的情况
            if '}{' in res.text:
                jtext = '{' + res.text.split('}{')[1]
                print("阿B返回了两段json, 取第二段")
            else:
                jtext = res.text
            # 解析json字串，防止返回的东西不对
            try:
                resj = json.loads(jtext)
            except:
                resj = {"code":100, "data":{"name":"~unknow~"}}
            # 根据错误码情况处理
            if not resj["code"] == 200:
                # 成功
                if resj["code"] == 0:
                    upinfo = {}
                    upinfo["uid"]   = int(uid)
                    upinfo["uname"] = resj["data"]["name"]
                    upinfo["group"] = [group]
                    upinfo["watch"] = True
                    upinfo["islucky"]= True
                    upinfo["ad_keys"]= []
                    upinfo["live"] = True

                    up_group_info[uid]=upinfo
                    try:
                        with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                            json.dump(up_group_info, f, ensure_ascii=False)  
                        up_latest[uid]=[]
                        live_latest[uid]=0
                        up_history_write(uid)
                        print(f'add {upinfo["uname"]}({uid}) history json to {up_dir+uid}.json')
                        up_list = list(up_group_info.keys())
                    except Exception as e:
                        msg="UP主数据写入失败，请查看日志。"
                        log.info(f'关注失败,无法修改list文件或无法创建用户记录文件，详细信息为:{e}')
                        return False,msg
                    
                    msg=f'{upinfo["uname"]}[{uid}]'
                    log.info(f'关注成功，群: {group}，用户: {up_group_info[uid]["uname"]}({uid})')
                    return True, msg

                # wbi认证失败
                elif resj["code"] == -403:
                    await wbi.update()
                    log.info(f'API返回"-403 访问权限不足"，即将更新密钥然后尝试：{abs(4-retry_time)}')
                    retry_time -=1
                    # time.sleep(3)
                    await asyncio.sleep(3)
                    
                # 服务器返回其他错误码
                else:
                    log.info(f'API返回(code={resj["code"]}, message={resj["message"]})，即将重试：{abs(4-retry_time)}')
                    retry_time -=1
                    # time.sleep(3)
                    await asyncio.sleep(3)
            else:
                # 服务器返回200，查无此人
                msg = "UID有误。"
                log.info(f'关注失败，查无此人(uid={uid})')
                return False, msg

    # 已经关注过了，那么只需要添加group
    else:
        if group in up_group_info[uid]["group"]:
            log.info(f'关注失败，已经关注过了')
            msg = "已经关注过惹~"
            return False,msg
        else:    
            up_group_info[uid]["group"].append(group)
            up_group_info[uid]["watch"]=True
            try:
                with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:
                    json.dump(up_group_info, f, ensure_ascii=False)
            except:
                log.info('关注失败,无法修改list文件或无法创建用户记录文件')
                return False, "UP主文件写入失败，未知错误，请检查数据文件。"
            msg=f'{up_group_info[uid]["uname"]}[{uid}]'
            return True, msg
    

def follow_illegal(uid, group, rawmsg):
    global number,up_latest, up_dir, up_list, gcookies,allow_follow_illegal
    if allow_follow_illegal:
        upinfo = {}
        upinfo["uid"]   = int(uid)
        upinfo["uname"] = "null"
        upinfo["group"] = [group]
        upinfo["watch"] = True
        upinfo["islucky"]= True
        upinfo["ad_keys"]= []
        upinfo["live"] = True

        up_group_info[uid]=upinfo
        try:
            with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                json.dump(up_group_info, f, ensure_ascii=False)  

            up_latest[uid]=[]
            live_latest[uid]=0
            up_history_write(uid)
            up_list = list(up_group_info.keys())

        except Exception as e:
            msg="UP主数据写入失败，请查看日志。"
            log.info(f'关注失败,无法修改list文件或无法创建用户记录文件，详细信息为:{e}')
            return False,msg
        log.info(f'关注流程失败,按用户配置开始记录uid')
        return True, f'{rawmsg},【记录uid】'
    else:
        return False, rawmsg

def unfollow(uid, group):
    global number,up_latest, up_list
    """取关UP主，并更新有关文件

    Args:
        uid (num): 被取关的UP主ID
        group (num): 申请取关的群

    Returns:
        bool: 执行结果。
        str:  结果信息。
    """
    rst = False
    msg = "未知错误。"
    if not uid.isdigit():
        msg = '请输入正确的UID!'
        log.info(f'取关失败，UID错误: "{uid}"')
    else:
        if uid not in up_list:
            msg="没有关注ta哦~"
            log.info(f'取关失败，该用户({uid})从未添加。')
        else:
            if group not in up_group_info[uid]["group"]:
                msg="没有关注ta哦~"
                log.info(f'取关失败，该群({group})未关注用户({uid})')
                log.debug(f'用户{uid} 被关注的群包含{up_group_info[uid]["group"]}')
            else:
                try:
                    up_group_info[uid]["group"].remove(group)
                    with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:
                        json.dump(up_group_info, f, ensure_ascii=False)
                    # del up_latest[uid]    # 出错，取关导致up主被动态历史的列表清除，实际上不关注的人也会进这个列表
                except:
                    log.info('取关失败,无法修改list文件')
                    return False, "UP主文件修改失败，未知错误，请手动检查配置文件。"
                msg = f'已经取关{up_group_info[uid]["uname"]}({uid})惹~'
                rst = True
                log.info(f'取关成功，群: {group}，用户: {up_group_info[uid]["uname"]}({uid})')
    return rst, msg

# 直播间检查，找开播的人
async def live_check(): # sync to async
    global up_group_info, up_list, live_latest, up_dir, conf, number_live, flag_number_live
    rst=-1000
    dylist=[]

    url='https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids'
    header={
        'Content-Type': 'application/json',
        'accept':'*/*',
        'user-agent':'curl/7.0.0'
        }
    data = json.dumps({'uids':up_list})
    async with async_client(proxies=p) as client:
        res = await client.post(url=url, data=data, headers=header)
        # res = await client.get(url=url+'?'+para, headers=header)
    # res = requests.post(url=url, data=data, headers=header)
    if not res.status_code == 200:
        log.warning(f'直播间查询失败，服务器返回{res.status_code}')
        return 0, []
    try:
        result = res.json()
    except json.JSONDecodeError:
        log.warning(f'直播间查询失败，返回内容不是JSON：status={res.status_code}, body={res.text[:300]}')
        return 0, []
    if(result["code"] != 0):
        log.warning(f'直播间查询失败，错误为{result["msg"]}')
        return 0,[]
    rooms = result["data"]
    
    for uid in rooms.keys():
        room = rooms[uid]
        status = room["live_status"]
        if live_latest[uid] == status or abs(live_latest[uid]-status) == 2:
            # 状态不变时直接跳过。如果从停播切换到轮播，也跳过。
            continue
            
        if status == 1:       # 0未开播，1直播，2轮播
            # 开始直播
            # 检查是否发布通知，默认都发布
            thisup = up_group_info[uid]
            if "live" in thisup:
                if not thisup["live"]:
                    # 该up不纳入监控范围
                    continue
            if not thisup["watch"]:
                # 未被关注，跳过
                continue
            # 过筛，更新状态
            log.info(f'[LIVE] {up_group_info[uid]["uname"]}({uid}) 开始直播。 state= {live_latest[uid]} --> {status}')
            live_latest[uid] = 1
            up_history_write(uid)
            with open(up_dir+uid+'.json','r', encoding='UTF-8') as f:
                j = json.load(f)
            if 'skin' in j:
                skin = j['skin']
            else:
                skin=None
            # 完全处理json
            live = drawCard.Live(room)
            drawBox = drawCard.Box(conf)       # 创建卡片图片的对象
            roomimg,roomtype = await live.draw(drawBox, skin, False)

            roominfo = {
                "nickname": live.nickname,
                "uid":      live.uid,
                "type":     roomtype    ,
                "subtype":  "",
                "time":     time.time(),         # 开播为即时消息，时间戳不重要
                "pic":      roomimg,
                "link":     f'https://live.bilibili.com/{live.roomid}',        #https://live.bilibili.com/528
                "sublink":  "",
                "group":    thisup["group"]
            }
            dylist.append(roominfo)
            rst -= 1

        elif status in [0, 2]:
            log.info(f'[LIVE] {up_group_info[uid]["uname"]}({uid}) 下播了。 state= {live_latest[uid]} --> {status}')
            live_latest[uid] = status
            up_history_write(uid)

    return rst, dylist


async def shell(group, para, right):
    """类指令的热管理工具

    Args:
        group (num): 发起设置的群号
        para (str): 完整指令
        right (bool): 权限判断。
    """
    global up_group_info, up_list
    rst = True
    msg = '指令有误，请检查! "bili-ctl help" 可以查看更多信息'
    try:
        cmd = para[0]
    except:
        cmd = "help"
    paranum = len(para)

    log.info(f'--指令控制--  功能:{cmd}, 参数:{para[1:]}, 权限:{right}')

    if cmd == "black-words":
        rst, msg = await cmd_blklist(group, para, right)
    elif cmd == "islucky":
        rst, msg = await cmd_islucky(group, para, right)
    elif cmd.upper() == "RELOAD":
        if not right:
            return False, "你没有权限这么做"
        with open(join(up_dir,'list.json'), 'r', encoding='UTF-8') as f:
            up_group_info = json.load(f)
            up_list = list(up_group_info.keys())
        msg = "信息更新完成!"
    elif cmd == "add-nick":
        rst, msg = await cmd_nick(group, para, right, 'add')
    elif cmd == "del-nick":
        rst, msg = await cmd_nick(group, para, right, 'del')
    elif cmd == "list-nick" or cmd == "ls-nick":
        rst,msg = await cmd_nick(group, para, True, 'list')

    elif cmd == "help":
        msg = help_info

    msg = msg.replace("'", '')
    msg = msg.replace('[','')
    msg = msg.replace(']','')
    print(f'bili-ctl return msg: {msg}')
    return rst, msg


def get_follow(group:int, level:int=0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (int):    查询的群号
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    count = 0
    txt = "本群已关注：\r\n"
    for uid in up_group_info.keys():
        if group in up_group_info[uid]["group"]:
            txt += f'{up_group_info[uid]["uname"]}({uid})'
            if level >= 2:
                txt += f'\r\n  是否过滤转发抽奖: {up_group_info[uid]["islucky"]}'
                txt += f'\r\n  过滤关键词有: {str(up_group_info[uid]["ad_keys"])}'
            if level >= 9:
                txt += f'\r\n  关注的群号有: {str(up_group_info[uid]["group"])}'
            txt += '\r\n'
            count +=1

    rst = True if count else False
    info = txt+f'共{count}位UP主' if count else "本群未关注任何UP主！"
    return rst, info


def get_follow_byuid(group:str, level:int=0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (str):    str输入all，将会显示所有的up主，包含watch=false的
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    if not group == "all":
        return False, "函数参数错误，仅接受'all'"
    count = 0
    txt = "本bot已关注：\r\n"
    for uid in up_group_info.keys():
        txt += f'{up_group_info[uid]["uname"]}({uid})'
        if level >= 9:
            txt += f'\r\n  是否过滤转发抽奖: {up_group_info[uid]["islucky"]}'
            txt += f'\r\n  过滤关键词有: {str(up_group_info[uid]["ad_keys"])}'
        if level >= 2:
            txt += f'\r\n  群号: {str(up_group_info[uid]["group"])}'
        txt += '\r\n'
        count += 1
    rst = True if count else False
    info = txt+f'共{count}位UP主' if count else "您还没有关注任何UP主。"
    return rst, info
    
def get_follow_bygrp(group:str, level:int=0):
    """获得该群关注的UP的昵称和uid，调整level可以获得完整信息

    Args:
        group (str):    str输入all，将会显示所有的up主，包含watch=false的
        level (int):    显示信息等级，具体为
                                level 0: nickname(uid)
                                level 2: nickname(uid)-islucky-ad_keys
                                level 9: nickname(uid)-islucky-ad_keys-groups

    Returns:
        rst (bool):     执行结果。出错、未关注任何人返回false
        info (str):     关注的信息。或者错误信息。
    """
    count = 0
    txt = "群关注列表汇总：\r\n"
    lists={}
    # 遍历up主，把uid分类到群信息
    for uid in up_group_info.keys():
        for grp in up_group_info[uid]["group"]:
            if grp in lists.keys():
                lists[grp].append(uid)
            else:
                lists[grp]=[uid]
        count += 1
    # 按群生成文字消息
    for g in lists:
        txt += f'群{g}已关注:\r\n'
        for u in lists[g]:
            txt+=f'  {up_group_info[str(u)]["uname"]}({u})\r\n'
        txt += '\r\n'

    rst = True if count else False
    info = txt[0:-2] if count else "还没有关注任何UP主。"
    return rst, info


async def guess_who(keywds:str):
    """利用搜索功能，猜测昵称指代的用户
        该功能效率和成功率都低，谨慎使用。
        每个用户增加昵称的配置项，匹配时优先全匹配gw_nick_list，然后模糊匹配gw_name_list，
        最后利用b站的搜索API进行搜寻，返回第一个结果。
        匹配结束后，不会保存，请调用另一个接口

    Args:
        keywds (str): 关键词

    Returns:
        uid (int):      查询的uid结果，匹配失败=0
        uname (str):    查询的全名结果，匹配失败=空字符串
        nick (str):     输入的短昵称，返回原样
        lev (float):    查询的等级，1表示完全一致，<1表示相似性，用于判断是否加入昵称列表。
    """
    uid, who,lev = 0, '', 0.0
    if keywds in gw_nick_list:
        who = gw_nick[keywds]["uname"]
        lev = 1.0
        uid = gw_nick[keywds]["uid"]
        log.info(f'GuessUP: 搜索于 1-已有昵称列表, 关键词[{keywds}] ==> {who}({uid}) level=1.0')
        return uid, who, keywds, lev
    
    maybe = difflib.get_close_matches(keywds, gw_name_list)
    # print(maybe)
    if maybe:
        who = maybe[0]
        lev = max(difflib.SequenceMatcher(None, who, keywds).quick_ratio(), \
                difflib.SequenceMatcher(None, keywds, who).quick_ratio())
        lev = float(int(lev*100))/100
        uid = gw_user[who]["uid"]
        log.info(f'GuessUP: 搜索于 2-关注列表相似, 关键词[{keywds}] ==> {who}({uid}) level={lev}')
        return uid, who, keywds, lev
    
    else:
        uid, who = await search_up_in_bili(keywds)
        if uid:
            lev = max(difflib.SequenceMatcher(None, who, keywds).quick_ratio(), \
                    difflib.SequenceMatcher(None, keywds, who).quick_ratio())
            lev = float(int(lev*100))/100
            log.info(f'GuessUP: 搜索于 3-B站搜索页, 关键词[{keywds}] ==> {who}({uid}) level={lev}')
            return uid, who, keywds, lev
        else:
            log.info(f'GuessUP: 所有途径搜索失败。关键词[{keywds}] ==> Nothing!')
            return uid, who, keywds, lev


def save_uname_nick(uid:str, uname:str, nick:str):
    """保存用户昵称

    Args:
        uid (str): 用户id
        uname (str): 用户名，没啥用，就二次确认一下
        nick (str): 要记录的昵称

    Returns:
        res (str/None):  错误信息,成功为空None
    """
    global up_group_info,gw_name_list,gw_nick_list,gw_user,gw_nick
    # 该昵称是否被人用过
    if nick in gw_nick_list:
        if gw_nick[nick]["uname"] == uname:
            return None
        else:
            log.info(f'保存昵称信息：失败，名称冲突。 {nick}已被 {gw_nick[nick]["uname"]}({gw_nick[nick]["uid"]}) 占用，{uname}无法使用。')
            return f'该昵称已被 {gw_nick[nick]["uname"]}({gw_nick[nick]["uid"]}) 占用'

    if not up_group_info[uid].get("nick"):
        up_group_info[uid]["nick"] = []
    up_group_info[uid]["nick"].append(nick)
    try:
        with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
            json.dump(up_group_info, f, ensure_ascii=False)
    except:
        up_group_info[uid]["nick"] = nick
        return "配置文件保存失败"
    # 更新内存中的配置
    for id in up_group_info:
        u = up_group_info[id]
        if u.get("nick"):
            gw_user[u["uname"]] = {"uid":u["uid"], "nick":u["nick"]}
            for n in u["nick"]:
                gw_nick[n] = {"uname":u["uname"], "uid":u["uid"]}
        else:
            gw_user[u["uname"]] = {"uid":u["uid"], "nick":[]}
    gw_name_list = gw_user.keys()
    gw_nick_list = gw_nick.keys()
    log.info(f'保存昵称信息：成功')
    return None

def del_uname_nick(uid:str, uname:str, nick:str):
    """删除用户昵称。注意，本功能会验证uid，但不进行用户名验证，遇到不存在的用户名会出错。

    Args:
        uid (str): 用户id
        uname (str): 用户名，没啥用，就二次确认一下
        nick (str): 要记录的昵称

    Returns:
        res (str/None):  错误信息,成功为空None
    """
    global up_group_info,gw_name_list,gw_nick_list,gw_user,gw_nick
    if nick in gw_nick_list:
        if uid not in up_list:
            return "该用户未关注"
        if gw_nick[nick]["uname"] == uname:
            up_group_info[uid]["nick"].remove(nick)
            try:
                with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                    json.dump(up_group_info, f, ensure_ascii=False)
            except:
                up_group_info[uid]["nick"] = nick
                return "配置文件保存失败"
            # 更新内存中的配置
            for id in up_group_info:
                u = up_group_info[id]
                if u.get("nick"):
                    gw_user[u["uname"]] = {"uid":u["uid"], "nick":u["nick"]}
                    for n in u["nick"]:
                        gw_nick[n] = {"uname":u["uname"], "uid":u["uid"]}
                else:
                    gw_user[u["uname"]] = {"uid":u["uid"], "nick":[]}
            gw_name_list = gw_user.keys()
            gw_nick_list = gw_nick.keys()
            return None
        else:
            return '该用户无此昵称'
    else:
        return "这个昵称未被使用。"

#====================附加功能，外部请勿调用======================
# 每日清理垃圾，减少文件占用，减少内存占用
def clean_cache():
    global up_latest, up_dir
    img_cache = conf.getint('cache', 'image_cache_days')
    dy_cache  = conf.getint('cache', 'dycard_cache_days')
    dy_flag = conf.getboolean('cache', 'dycard_cache')
    if img_cache > 0:
        cache_clean_time_point = time.time() - img_cache*3600*24
        dirname = ["image", "cover", "article_cover"]
        for t in dirname:
            for root, dirs, files in os.walk(join(curpath,f'res/cache/{t}')):
                for f in files:
                    full__path_file = join(root, f)
                    if getmtime(full__path_file) < cache_clean_time_point:
                        try:
                            os.remove(full__path_file)
                        except Exception as error:
                            log.error(f'Err while clean image cache: {f} in "{t}"!')
        log.info(f'Clean image cache finish!')
    if dy_cache > 0 and dy_flag:
        cache_clean_time_point = time.time() - dy_cache*3600*24
        dirname = "dynamic_card"
        if exists(join(curpath, dirname)):
            for root, dirs, files in os.walk(join(curpath,f'res/cache/{dirname}')):
                for f in files:
                    full__path_file = join(root, f)
                    if getmtime(full__path_file) < cache_clean_time_point:
                        try:
                            os.remove(full__path_file)
                        except Exception as error:
                            log.error(f'Err while clean dynamic cache: {f} in "{dirname}"!')
        log.info(f'Clean dynamic cache finish!')

    for uid in up_list:
        l = len(up_latest[uid])
        if  l > 21:
            try:
                up_latest[uid] = up_latest[uid][(l-21):]        # 清理文件的同时清理内存
                up_history_write(uid)                
            except:
                log.error(f'Err while clean history: {uid}')
    log.info('Clean uppers history finish!')


def m2hm(t:int):
    ms = t//60
    t = f'{int(ms//60)}h{int(ms%60)}m' if ms>60 else f'{ms} minutes'
    return t

async def check_plugin_update():
    # 检查代码是否更新。由于现阶段代码会频繁更新，所以添加这个定期检查功能。
    # version.json内容：{"ver":"0.x.x", "date":"2022-07-01", "desc":["更新了版本检查功能，仅在日志里输出"]}
    way = conf.getint('common','if_check_update')
    if way == 1:
        url = 'https://gitee.com/kushidou/bili-notice-hoshino/raw/main/version.json'
    elif way == 2:
        url = 'https://github.com/kushidou/bili-notice-hoshino/raw/main/version.json'
    else:
        return
    myverpath = join(curpath,'version.json')
    myver = 'old'
    # 获取本地版本。不存在version文件则视为极旧版本
    if exists(myverpath):
        try:
            with open(myverpath, 'r') as f:
                mytxt = json.load(f)
                myver = mytxt["ver"]
                log.info("例行检查更新,从version.json获取版本号")
        except:
            myver = 'old'
            log.info("从version.json获取版本号失败")
    else:
        log.info("例行检查更新,但是version.json不存在")
        with open(myverpath, 'w', encoding='UTF-8') as f:      # 更新UP主列表
            json.dump({"ver":"old"}, f, ensure_ascii=False)
        
    try:
        async with async_client(proxies=p) as client:
            res = await client.get(url=url, follow_redirects=True)
        # res = requests.get(url)
    except:
        log.error(f'Check update failed! Please check your network.')
        return
    if res.status_code == 200:
        try:
            txt = res.json()
        except json.JSONDecodeError:
            log.error(f'Check update failed! Response is not JSON: {res.text[:300]}')
            return
        newver = txt["ver"]
        if not newver == myver:
            date = txt["date"]
            desc = txt["desc"].replace("\n", "\n\t\t\t\t\t\t")
            log.info(f'bili-notice-hoshino插件已更新, 请至github主页拉取最新代码。\n \
                \t地址:  https://github.com/kushidou/bili-notice-hoshino  \n   \
                \t当前版本 {myver}, 最新版本号 {newver}, 更新时间{date}\n\
                \t更新内容:  {desc}')
            return
    else:
        log.error(f'Check update failed! HTTP code = {res.status_code}')
        return

async def search_up_in_bili(keywds:str):
    global gcookies
    """到b站搜索up主，并返回最接近的信息

    Args:
        keywds (str): 输入的关键词

    Returns:
        uid (int):  搜索到的uid
        who (str):  对应的昵称
    """
    uid, who = 0, ""
    url = "https://api.bilibili.com/x/web-interface/search/type"
    header=bili_web_headers(f'https://search.bilibili.com/upuser?keyword={keywds}')
    para={"search_type":"bili_user", "keyword":keywds}
    try:
        async with async_client(proxies=p) as client:
            res = await client.get(url=url, params=para, cookies=gcookies,headers=header)
        # res = requests.get(url=url, params=para, cookies=gcookies)
    except Exception as e:
        log.error(f'搜索UP主失败，原因为网络错误：{e}')
        return uid, who
    if res.status_code == 200:
        try:
            resj = res.json()
        except json.JSONDecodeError:
            log.error(f'搜索UP主失败，返回内容不是JSON：status={res.status_code}, body={res.text[:300]}')
            return uid, who
        if not resj["data"]["numResults"] == 0:
            usr = resj["data"]["result"][0]
            who = usr["uname"]
            uid = usr["mid"]
        else:
            log.error(f'搜索UP主失败，原因为 没有搜索到有关结果')
    else:
        log.error(f'搜索UP主失败，原因为 return code == {res.status_code}')
    return uid, who

async def cmd_blklist(group, para, right):
    rst = True
    msg = ""
    paranum = len(para)
    if paranum >= 3:
        uid = para[1]
        fun = para[2]
        if uid not in up_list:
            msg = 'UP主未关注,请检查uid!'
        else:
            if fun == "list":
                uname = up_group_info[uid]["uname"]
                msg = f'您已经为 {uname} 设置了以下过滤关键词：\r\n{up_group_info[uid]["ad_keys"]}'
            elif fun == "add":
                if not right:
                    return False, "你没有权限这么做"
                if paranum >3:
                    keys = para[3:]
                    try:
                        up_group_info[uid]["ad_keys"].extend(keys)
                        with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                            json.dump(up_group_info, f, ensure_ascii=False)
                        msg = f'添加成功.'
                    except:
                        msg = f'添加失败'
            elif fun == "remove":
                if not right:
                    return False, "你没有权限这么做"
                if paranum>3:
                    keys = para[3:]
                    erkeys=[]
                    for wd in keys:
                        try:
                            up_group_info[uid]["ad_keys"].remove(wd)
                        except:
                            erkeys.append(wd)
                    with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                        json.dump(up_group_info, f, ensure_ascii=False)
                    msg = '移除成功。'
                    if erkeys:
                        msg = msg+f'以下关键词移除失败，可能是没有这些关键词:\n{erkeys}'
    else:
        rst = False
        msg = "参数有误"
    return rst,msg

async def cmd_islucky(group, para, right):
    paranum = len(para)
    if not right:
        return False, "你没有权限这么做"
    if paranum == 3:
        uid = para[1]
        fun = para[2]
        if uid not in up_list:
            msg = 'UP主未关注,请检查uid!'
        else:
            msg = f'已为 {up_group_info[uid]["uname"]} 更新抽奖开奖动态的设置。'
            if fun.upper() == "TRUE":
                up_group_info[uid]["islucky"] = True
            elif fun.upper() == "FALSE":
                up_group_info[uid]["islucky"] = False
            else:
                msg = "参数错误，请重试。"
            with open(join(up_dir,'list.json'), 'w', encoding='UTF-8') as f:      # 更新UP主列表
                        json.dump(up_group_info, f, ensure_ascii=False)
        return True, msg
    else:
        return False, "参数有误"

async def cmd_nick(group, para, right, fun):
    paranum = len(para)
    if not right:
        return False, "你没有权限这么做"
    if paranum == 3:
        u=para[1]
        n=para[2]
        if u.isdigit():
            uid = u
            uname = up_group_info[uid]["uname"]
        else:
            uid, uname, _, lev = await guess_who(u)
            if lev <1:
                return False, "未找到该用户"
        if fun == 'add':
            rst = save_uname_nick(str(uid), uname, n)
            print(rst)
            return True, rst if rst else "成功"
        elif fun == "del":
            rst = del_uname_nick(str(uid), uname, n)
            print(rst)
            return True, rst if rst else "成功"
    if paranum == 2 and fun == "list":
        u=para[1]
        if u.isdigit():
            uid = u
            uname = up_group_info[uid]["uname"]
        else:
            uid, uname, _, lev = await guess_who(u)
            if lev <1:
                return False, "未找到该用户"
        ruid = gw_user[uname]["uid"]
        rnick= gw_user[uname]["nick"]
        if len(rnick):
            msg = f'{uname}({ruid})的昵称有：\r\n'
            for n in rnick:
                msg+=f'{n}\r\n'
        else:
            msg = f'{uname}({ruid}) 还没有昵称，请设置。\r\n'
        return True,msg[0:-2]
        
    else:
        return False, "参数有误"
