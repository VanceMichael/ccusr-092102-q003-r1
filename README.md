# 晋戏影像沿革档案

服务于山西戏曲长期影像资料（二十余年田野拍摄，覆盖 38 个剧种）的整理、勘误与分级开放。
领域资料把**影像文件指纹、拍摄批次、地点沿革、剧种分类版本、剧目、人物/团体别名、说明文本、资料出处**视为相互独立且全程可溯的记录。

## 领域规则

- **幂等回传**：影像按「拍摄批次 + 离线端记录键」去重；`sha256:` 等文件指纹跨批次查重。重复导入返回既有记录，不产生第二条。
- **只追加的勘误**：说明文字与剧种归类永不就地覆盖。胶片卡片原话是第 1 版，口述与出版的后续说法各自成版，每版必须附**资料出处**与**勘误依据**。任一条记录都能回到最初说明并查看每次勘误依据。
- **名称只作线索**：戏班改名、演员跨剧种流动通过别名/曾用名解析；同名歧义和「疑似同一人/戏班/场所」只生成 `pending` 候选关系，馆员确认后才合并（旧名、改挂痕迹保留），驳回则二者并存。
- **场所/团体沿革**：更名后旧名自动进入曾用名，并保留带日期与出处的沿革记录。
- **许可按权利方与用途核对**：用途分研究、展览、网络传播、出版；权利方为作者（摄影者）、参演演员、院团，出版另有出版方。任一缺失或撤回即不得使用。
- **撤回只限未来**：撤回（支持按用途部分撤回）只影响此后的使用核对；已登记的出版/展出为不可变留痕，并快照登记当时的核对结论。
- **研究者检索视图**：按剧种检索时，获准影像可见完整元数据与当前说明；受限影像只给指纹、批次等最小信息并显式标注「受限」及缺失权利方；未确认身份的参演标注 `unconfirmed`，不呈现为定论。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 22 个用例
PYTHONPATH=src python3 -m jin_opera_archive  # 默认 0.0.0.0:8080，ARCHIVE_DB 指定数据文件
```

仅依赖 Python 3.12 标准库；数据以单个 JSON 文件原子写入（临时文件 + `os.replace`）。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 登记拍摄批次（同 code 重复提交幂等） |
| POST | `/entities/{persons,organizations,places,genres,plays}` | 登记实体（均须 `source`） |
| POST | `/entities/{coll}/{id}/aliases` | 补别名/曾用名 |
| POST | `/places/{id}/rename`、`/organizations/{id}/rename` | 更名沿革（`name`/`since`/`source`） |
| POST | `/assets/import` | 幂等导入影像（指纹、批次、名称/编号、说明、演员） |
| POST | `/assets/{id}/captions` | 说明勘误（`text`/`source`/`basis`） |
| POST | `/assets/{id}/genre-classifications` | 剧种归类勘误 |
| GET | `/assets/{id}/provenance`、`/provenance/{kind}/{id}` | 全版本与沿革溯源 |
| GET/POST | `/candidates`、`POST /candidates/{id}/resolution` | 候选关系列表/人工确认合并或驳回 |
| POST | `/assets/{id}/licenses` | 授权（同权利方重复登记按用途取并集） |
| POST | `/licenses/{id}/withdraw` | 全部或按用途撤回（仅影响未来） |
| GET | `/assets/{id}/permission?purpose=web[&publisher_id=...]` | 按用途核对，列出已授与缺失权利方（出版登记时核出版方） |
| POST | `/assets/{id}/usages` | 出版/展出等使用留痕（不可变，含授权快照） |
| GET | `/search?genre=晋剧&purpose=research` | 研究者剧种检索（获准细节/受限标记） |

`fixtures/archive_item.json` 是一条最小影像记录样例。
