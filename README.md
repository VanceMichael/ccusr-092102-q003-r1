# 晋戏影像沿革档案

本项目服务于山西戏曲长期影像资料（二十余年、38 个剧种）的整理与开放。领域资料将影像文件、拍摄批次、拍摄地点、剧种分类、人物与团体别名、说明文字、资料出处和传播许可视为相互独立但可追溯的记录。

## 数据模型（分实体保存）

| 事实 | 表 |
| --- | --- |
| 底片/数字文件指纹 | `assets`（指纹唯一，内容寻址 `asset-<hash>`） |
| 拍摄批次 | `batches`（摄影者、起止时间、田野笔记） |
| 地点沿革 | `places` + `place_names`（每段名称带有效期与出处） |
| 剧种分类版本 | `genre_schemes` + `genre_scheme_entries` + `asset_genres`（同一影像在不同分类方案下的标注并存，订正只置 `superseded` 不删除） |
| 剧目 | `plays` + `play_aliases` |
| 人物/团体别名 | `persons`/`troupes` + 各自别名录（团体别名带有效期，戏班改名即新增一段别名） |
| 说明文本 | `captions`（按版本只增不改，每版记录依据与作者） |
| 资料出处 | `sources`（胶片卡片、口述、出版物等，内容寻址幂等） |
| 许可 | `licenses`（主体×用途×影像范围，含授予与撤回时间） |
| 使用留痕 | `usages`（出版/展出/网络发布登记，附许可快照） |
| 候选关系 | `candidates`（疑似同一，待人工判断） |
| 审计 | `events`（追加式事件日志：操作者、时间、依据、出处） |

## 六条档案纪律

1. **幂等回传**：`POST /imports` 以 `import_key` 登记台账，重复回传直接返回首次结果；文件指纹、出处、说明版本、候选关系均按内容寻址，换批次重复导入同一内容不产生重复记录。说明文字与已存版本冲突时只告警，不擅自生成新版本。
2. **候选关系**：导入时遇到人名、戏班名、场所名只记录「提及」并生成 `pending` 候选关系，绝不自动挂接或合并。馆员确认后由服务完成链接或合并（合并保留双方记录与 `merged_into` 指向，全程留痕）。
3. **许可核对**：授权要求影像的全部主体——作者、演员、院团、出版方——在该用途（展览/研究/网络传播/出版）下均有有效许可；作者身份未确认即不可授权。
4. **撤回语义**：撤回只写入 `withdrawn_at`，限制撤回时点之后的授权；此前已登记的出版与展出继续留痕，撤回时点之前的授权判断不受影响。
5. **检索可见性**：研究者按剧种检索时，未获许可的影像只返回可识别的占位信息（编号、年代、批次、受限原因），不返回说明文本与指纹；未确认身份以 `identity_unconfirmed` 标记。馆员视图可见全部内容与待决候选。
6. **溯源**：`GET /assets/{id}/history` 返回事件流与说明全部版本，v1 即最初说明，每次勘误的依据与出处随版本保存；`GET /lineage/{type}/{id}` 可查任意记录的事件链。

## 运行

```bash
# 检查（unittest；安装 pytest 后亦可 python3 -m pytest tests）
python3 -m unittest discover -s tests -v

# 启动服务（纯标准库，无第三方依赖）
PYTHONPATH=src python3 -m jin_opera_archive [host] [port] [db_path]
```

## 主要接口

- `POST /imports` 田野批次幂等导入；`GET /imports/{key}` 查台账
- `POST /assets/{id}/captions` 登记勘误（必须附依据）；`GET /assets/{id}/captions|history|usages|authorization`
- `POST /persons|/troupes|/places|/genres|/genre-schemes` 权威实体建档；`POST /troupes/{id}/rename` 戏班改名；`POST /places/{id}/names` 地点沿革
- `GET /candidates` 待决候选；`POST /candidates/{id}/resolve` 人工确认/否决
- `POST /licenses` 授予许可；`POST /licenses/{id}/withdraw` 撤回（仅限制未来）
- `POST /assets/{id}/usages` 登记使用（许可不足返回 409）
- `GET /search?genre=…&purpose=…&viewer=researcher|archivist` 按剧种检索

`fixtures/archive_item.json` 是一条最小影像记录；`fixtures/field_batch.json` 是一份田野回传批次样例。真实资料中的原始文件指纹、早期说明和后续勘误都应长期保留。
