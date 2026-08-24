# playmaker: детекция «done, но ничего не сделано» (no-changes detection)

**Статус:** todo · **Приоритет:** high · **Затронуто:** `state.py`, `cli.py` (finalize + list/get/watch), `notify.py`, `agents/opencode.py` (адаптер как первый подозреваемый), `_skills` (playmaker-coach)

## Проблема

Воркер, которому дана задача **с записью файлов**, может завершиться с exit 0, вернуть текст — и playmaker пометит сессию `done`, пришлёт «✅ done» и посчитает батч успешным, хотя **в `--cwd` не изменён ни один файл**. Для оркестратора это неотличимо от успеха, пока кто-то не пойдёт смотреть на диск руками.

### Инцидент (2026-08-18)

- `playmaker dispatch opencode --model zai-coding-plan/glm-5.3 --cwd ~/Sites/emmet-compiler` с задачей на ~200 строк кода (reverse parser + тесты).
- Процесс жил 10 мин, `exit 0`, статус **`done`**, `cost $0.00`.
- Финальный вывод — 40 строк внутреннего анализа, оборванного на полуслове; `git status --short` в `--cwd` — **пусто**; `session_file` для `thread` не резолвится («no resolvable session_file»).
- Батч-сводка показала бы `1/1 done ✓`.
- Обнаружено только потому, что оператор вручную спросил «а он точно работает?» и coach проверил `git status`.

Аналогичный класс бага уже чинили для Codex (`agents/codex.py:102`: без last-message файла «dispatch would look done with no…»). Это тот же баг, но на уровне **результата**, а не транспорта, и он провайдер-независим.

### Почему это критично

Coach-паттерн держится на доверии к статусу: `--batch` суммирует, оркестратор читает `done` и идёт дальше. Ложный `done` = тихо потерянная задача + потерянное время + ложная уверенность выше по цепочке.

## Что сделать

### 1. Снимок рабочего дерева до/после (ядро)

При старте dispatch/continue (`cli.py`, до `handler.dispatch(...)`), если `--cwd` — git-репозиторий:
- сохранить `pre_tree_hash` = `git -C <cwd> status --porcelain --untracked-files=all | sha256` (плюс `HEAD` sha на случай, если агент коммитит).

При финализации (`cli.py:253`, ветка `status="done"`):
- посчитать `post_tree_hash` тем же способом; `files_changed` = число строк порchelain-диффа между до/после (или `git diff --stat` + untracked count).
- Если `--cwd` не git — fallback: список путей + mtime по дереву до/после (ограничить глубину/размер; достаточно `find -newer <marker-file>`).
- Писать в `sessions`: новые колонки `files_changed INTEGER`, `pre_tree_hash TEXT`, `post_tree_hash TEXT` (миграция в `state.py` рядом с `model/batch_id`).

### 2. Новый терминальный статус `no_changes`

- Если `files_changed == 0` **и** сессия помечена как write-task (см. п. 3) → `status="no_changes"` вместо `done`.
- `no_changes` — терминальный, входит во все `terminal = {...}` множества (`cli.py:323, 522, 639, 754`), в `state.py:90`, в `watcher.py` (иконка ⚠️), в `--status` фильтр `list`.
- В батч-сводке считается **не** успехом: `N/M done · opencode ⚠ no_changes`.
- Нотификация: отдельная, громкая (как для failed — Basso), текст «done but wrote 0 files in <cwd>», клик открывает output.

### 3. Как понять, что задача «с записью» (чтобы не шуметь на recon)

Три сигнала, любой достаточен; по умолчанию — эвристика:
- Явный флаг: `dispatch --expect-changes` / `--read-only` (`--read-only` подавляет проверку для recon/консилиума).
- Эвристика по промпту (дешёвая, включена по умолчанию): наличие слов `implement|add|create|write|edit|fix|refactor|build|feature|deliverable|acceptance` и отсутствие `recon only|do not edit|read-only|answer|reply with`. Ложноположительное = лишний ⚠, ложноотрицательное = как сейчас; лучше шуметь.
- Профиль агента (`.playmaker/agents/<name>.md`) может задавать default.

### 4. Диагностика в `get`/`summary`

`playmaker get <id>` показывает строку `changes: 0 files (⚠ no_changes)` / `changes: 7 files (+412/-18)`. `summary` — тоже. Так coach видит доказательство на диске, не ходя в `git status` сам.

### 5. Первый подозреваемый — opencode `--auto` и права записи — **ПОДТВЕРЖДЕНО (2026-08-18)**

Мини-репро выполнен: `dispatch opencode --model zai-coding-plan/glm-5.3 --sync --cwd <fresh git dir> --prompt "Create a file named hello.txt … containing OK. Reply DONE."` → агент ответил `DONE`, статус `done`, **файл не создан**. Значит проблема НЕ в масштабе задачи и не в модели: **opencode под playmaker не пишет в `--cwd` вообще.** Кандидаты (проверить по порядку в `agents/opencode.py`):
1. `--auto` не даёт write-permission (opencode.json пользователя может запрещать; проверить `permission` секцию и что именно auto-approve покрывает);
2. файл пишется в другой каталог — ловушка `PWD` vs `--dir` закрыта не полностью (поискать hello.txt по `~`, по scratch/`tempfile` адаптера, по `process.env.PWD` родителя);
3. модель GLM не вызывает write-tool под `--format json` (сравнить с другим провайдером через тот же адаптер, напр. `opencode --model anthropic/...` если настроен).
**КОРНЕВАЯ ПРИЧИНА НАЙДЕНА (2026-08-18, второй прогон):** GLM **пишет** (`tool: write`, `Wrote file successfully`) — но **теряет ведущий `/` в абсолютных путях**. В opencode.db у playmaker-сессии `ses_fea0b099…`: `filePath: "private/tmp/claude-501/…/pm-probe/hello.txt"` (без `/`), и файл найден по `<cwd>/private/tmp/…/pm-probe/hello.txt` — путь удвоился. Прямой запуск `opencode run … --auto` вне playmaker → GLM пишет `filePath: "hello.txt"` и всё верно. Третий пробник через playmaker с явно относительным путём (`world.txt`) → верно. Вывод: GLM склонен строить абсолютные пути из текста промпта/окружения и роняет слэш; opencode честно пишет «успешно» по кривому пути.

**Фикс в `agents/opencode.py` (сделать, а не обходить):**
1. **Промпт-преамбула** (как у agy — там уже есть workspace preamble): в `_build_prompt` добавлять первой строкой `Working directory: <cwd>. Use paths RELATIVE to it for every file operation; never construct absolute paths.` — устраняет соблазн у модели.
2. **Пост-проверка удвоения**: после завершения, если в `--cwd` появился каталог, чей путь начинается с `cwd/` + (cwd без ведущего слэша) — это симптом; либо (а) переместить содержимое на место и залогировать `path-doubling repaired`, либо (б) как минимум пометить сессию `no_changes`/`warning` и вывести путь. (а) предпочтительнее — иначе работа воркера теряется.
3. Sanity-тест в репо playmaker: dispatch opencode с абсолютным путём в промпте → файл на месте (после п.1/2).
4. Оставить в docstring адаптера: «GLM-5.3 drops the leading slash of absolute paths; keep prompts relative».

Отдельно: на задаче в ~200 строк GLM выдал 10 мин анализа и вышел без единого write-вызова — это второй, независимый режим отказа («додумал и вышел»); `no_changes`-детекция (п.1–2 выше) ловит оба.

### 6. Обновить skill `playmaker-coach`

В `_skills`: правило приёмки «после `done` на write-задаче — смотри `changes:` в `get`; `no_changes` = провал, редиспатч в другой лейн». Уже сегодня coach делает это руками — надо, чтобы делал инструмент.

## Acceptance

- [ ] Тест: dispatch с write-промптом, воркер не меняет файлы → статус `no_changes`, батч-сводка показывает ⚠, нотификация уходит с Basso.
- [ ] Тест: dispatch с `--read-only` и нулём изменений → статус `done`, тишина.
- [ ] Тест: воркер меняет 1 файл → `done`, `files_changed=1`, `get` показывает `changes: 1 file`.
- [ ] Тест: `--cwd` не git → fallback по mtime работает, не падает.
- [ ] `list --status no_changes` фильтрует; `watch` показывает ⚠.
- [ ] Мини-репро opencode из п. 5 выполнен, результат записан в docstring адаптера.
- [ ] Миграция `state.py` идемпотентна на существующей `state.db`.

## Вне скоупа

Семантическая проверка «сделано ли то, что просили» — нет. Только факт: файлы менялись или нет. Этого достаточно, чтобы убрать ложный `done`.
