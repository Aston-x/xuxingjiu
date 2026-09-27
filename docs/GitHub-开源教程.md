# 从本地目录到 GitHub 仓库：通用开源教程

这篇跟具体项目无关。不管你要发的是一个 Python 脚本、一个 TypeScript 库，还是一堆配置文件，从「我电脑上有个目录」到「GitHub 上有人搜得到、装得上、能提 issue」，路子是同一套。

**本文只管通用流程。** 本仓库怎么发（那三个 workflow 各守什么、这套一键安装脚本怎么打包、踩过的具体坑、QQ 号和密钥泄露那件事怎么收尾），写在 [开源指南.md](开源指南.md) 里。两篇有重叠但不冲突：要发这个项目，照那篇做；想搞懂原理和通用做法，看这篇。

文中提到本仓库的文件时给的是相对路径（本文在 `docs/` 下）。如果你手上的仓库已经有 CI，第 7 节大半可以直接跳过，我在那节写明了。

---

## 1. 装 git、配身份

```bash
git --version     # 没装就去 git-scm.com 下对应平台的安装包；macOS 也可以 brew install git
```

装完先配身份，不配的话第一次 commit 直接报错：

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main    # 让 git init 默认建 main，省得每次手动改名
git config --global core.quotepath false       # 中文文件名别显示成 \344\275\240 这种
```

邮箱这条有讲究。提交邮箱会公开写进每一条 commit 里（`git log` 一眼就能看到），爬虫会抓。不想暴露真实邮箱的话用 GitHub 给的 noreply 地址：在 Settings 的 Emails 页面能拿到 `数字+用户名@users.noreply.github.com` 这种形式。用它当提交邮箱，GitHub 照样把提交算到你头上，但别人看不到你真实邮箱。

已经提交过再改也行，但只改得动最后一条：

```bash
git config --global user.email "123456789+你的用户名@users.noreply.github.com"
git commit --amend --reset-author --no-edit    # 历史多的要 rebase 或 filter-repo 才能全改
```

改写历史会让已经克隆过的人对不上，所以趁仓库刚建、还没人 clone 的时候改最省事。

换行符这个坑值得单独讲。Windows 写出来的行尾是 CRLF，Linux/macOS 是 LF。同一个人换台机器提交，diff 里整个文件都成了改动，review 的人根本看不出你改了什么。稳妥做法是在仓库根放一个 `.gitattributes` 把规则写死，本仓库那份在 [../.gitattributes](../.gitattributes)，抄一份改改就能用。规则大概是：仓库里统一存 LF；shell 脚本必须 LF（CRLF 会把 `#!/bin/sh` 后面的 `\r` 当成命令名的一部分，报一堆莫名其妙的错）；`.bat` / `.cmd` 反过来必须 CRLF，老 cmd 解析 LF 的批处理会出问题；图片字体这类二进制别做任何转换。

大文件和二进制：

- 图片、字体、编译产物、模型权重、`node_modules`、打包好的 zip，都不该进版本库。git 存的是每个版本的完整快照，二进制改一个字节也要存一整份，仓库体积按版本数往上翻，别人 clone 还得全拉下来。
- 发布用的安装包、可执行文件挂到 Release 的 Assets 上（第 8 节），仓库里只放构建脚本。
- 已经误提交的大文件，删掉文件不够，历史里还留着，clone 下来照样那么大，得用 `git filter-repo` 清历史。
- 真的必须进的（比如一张小占位图），可以用 Git LFS。但开源项目先问自己一句：能不能不进。

`.gitignore` 就是干这个的：

```bash
.venv/
node_modules/
__pycache__/
dist/
*.log
.env
.vscode/
```

写完记得验，别写完就不管：

```bash
git status --ignored --short | grep '^!!'   # 看被忽略的都有哪些
git check-ignore -v 某个文件                 # 看它被哪条规则忽略了（排查"怎么没提交上去"）
```

---

## 2. 推之前先自查

一旦推上去，就别指望删干净了。GitHub 上删了文件，历史还在，fork 里、别人的本地克隆里也还有。所以密钥这类东西在按 push 之前查一遍最划算。

```bash
# 常见密钥形态
grep -rInE "sk-[A-Za-z0-9_-]{20,}" . | grep -v node_modules
grep -rInE "(api[_-]?key|access_token|secret|password)\s*[:=]\s*[\"'][^\"']{16,}" . | grep -v node_modules

# 本机路径和用户名，泄露的是个人信息
grep -rInE "/home/[a-z]+|Users[\\\\/][A-Za-z]+" . | grep -v node_modules

# 别忘了历史：有人是先提交、后想起来的
git log --all -p | grep -nE "sk-[A-Za-z0-9]{20,}" | head
```

真查出来了，顺序不能反：

1. 先去对应平台吊销 / 轮换那个密钥。这步跟 git 没关系，但它是唯一能真正止损的动作。
2. 再用 [git filter-repo](https://github.com/newren/git-filter-repo) 清历史。`git filter-branch` 也能干，但慢且容易用错，官方文档自己都推荐前者。

```bash
# 把每个要抹掉的字符串写成一行映射，然后让 filter-repo 全局替换
# 注意：Windows / Git Bash 下别用 <(echo ...) 做进程替换，那个路径 filter-repo 读不到
printf 'sk-真实密钥==>REDACTED\n' > /tmp/replacements.txt
git filter-repo --replace-text /tmp/replacements.txt
```

改写历史之后本地所有 commit 的 hash 都变了，远端推不上去，只能 force push；仓库要是有人 fork 过，他们那边会全部冲突。所以最好在第一次 push 之前就把这步做完。

还有一种更省事的防线：密钥永远放环境变量，仓库里只放一份 `.env.example`，真值写进 `.env` 并且 `.env` 进 `.gitignore`。这样你压根没机会提交错。

这些 grep 会报一堆误伤（文档里的示例串、测试夹具里的假号码、依赖目录里的二进制），别看到输出就慌。把命中导出来人工扫一遍，比写一条谁都匹配不到的完美正则靠谱：

```bash
# 只看自己写的那些文件类型，把命中存下来慢慢看
grep -rInE "sk-[A-Za-z0-9_-]{20,}" . \
  --include='*.py' --include='*.js' --include='*.json' --include='*.md' \
  --include='*.ts' --include='*.yml' > /tmp/hits.txt
wc -l /tmp/hits.txt
```

还有两个时间点必须再查一遍，很多人栽在这两处：

- **把私有仓库改成公开之前。** 私有的时候觉得无所谓的东西（内网地址、同事邮箱、测试用的真实数据），一公开就是全网可见。改之前把上面几条 grep 和历史都跑一遍。
- **在 CI 或脚本里打印环境变量之前。** `env`、`printenv`、带 `-x` 的 shell 脚本，会把 GitHub 注入的 secrets 打进日志。日志同样是公开的，而且会存很久。

真正该做的事其实是在写代码的时候就绕开风险：密钥不进仓库、不进截图、不进 issue，日志里打到密钥的地方统一用 `***` 替换。等出事再清理，成本高一个量级。

---

## 3. 建账号和仓库

账号：注册一个，把两步验证开了（GitHub 现在对贡献代码的账号基本都要求 2FA）。用户名会进仓库 URL，改的话旧链接全失效，起的时候想清楚。

建仓库：右上角 `+` → New repository。

- **名字**：短、小写、连字符分隔（`my-thing`）。名字会进 URL，也是搜索关键词。
- **描述**：一句话说清「这是什么、给谁用」。它出现在搜索结果和仓库首页，比名字更影响别人点不点进来。
- **Public**：开源就选 Public，Private 的别人看不到，谈不上开源。
- 底下三个勾选框：Add a README file / Add .gitignore / Choose a license。**本地已经有仓库的，三个都别勾。** 勾了远端会先有几个提交，你本地一推就因为历史对不上被拒，还得 `git pull --allow-unrelated-histories` 绕一圈。这三个文件在本地自己写，好控制。

**Topics** 在仓库首页 About 那块，点齿轮能填。它是标签，直接影响 GitHub 搜索里你能不能被人搜到。想几个别人真会搜的词：语言名、框架名、用途，比如 `python` `cli` `telegram-bot`。仓库刚建就能填，不用等。

---

## 4. 第一次提交与推送

```bash
cd 你的项目目录

git init                     # 已经 init 过就跳过
git add -A
git status                   # 这一步别省，确认没有 .env / config.json 混进去
git commit -m "feat: 首个公开版本"
git branch -M main           # 老版本 git 默认是 master，统一成 main
```

然后接远端，HTTPS 和 SSH 选一条。

**HTTPS**：

```bash
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

第一次推会弹浏览器登录，或者让你输 token。GitHub 早就不能用账号密码推了，密码那一栏得填 Personal Access Token（Settings → Developer settings → Personal access tokens）。好处是零配置，坏处是换机器、token 过期都要重来。

**SSH**（配一次，长期省事）：

```bash
# 1. 生成密钥对。ed25519 是现在推荐的算法，比 rsa 短也更安全
ssh-keygen -t ed25519 -C "you@example.com"
# 一路回车。passphrase 可以不设（自己电脑），设了更安全但每次要输或配 ssh-agent

# 2. 打印公钥。.pub 结尾的是公钥，可以到处贴；不带 .pub 的是私钥，绝不能发给任何人
cat ~/.ssh/id_ed25519.pub
# Windows PowerShell: type $env:USERPROFILE\.ssh\id_ed25519.pub
```

复制那一整行（`ssh-ed25519 AAAA... you@example.com`），到 GitHub 的 Settings → SSH and GPG keys → New SSH key 粘进去，标题随便写。然后测一下：

```bash
ssh -T git@github.com
# 看到 "Hi <用户名>! You've successfully authenticated" 就通了
# 第一次会问 yes/no，输 yes

git remote add origin git@github.com:<用户名>/<仓库名>.git
git push -u origin main
```

`-u` 的意思是记住这条对应关系，之后在当前分支直接 `git push` 就行。

推完去仓库首页刷新，README 该渲染出来了。第一次推之后的检查：

- 有没有多出不想公开的文件？（`git ls-files | head -50` 数一遍）
- README 里的图片和相对链接显示正常吗？相对链接最容易写错。
- 默认分支是不是 main？Settings → Branches 里能看到。

几个常撞的报错：

| 现象 | 原因 |
| --- | --- |
| `rejected ... fetch first` | 远端有你本地没有的提交，多半是建仓库时勾了 README。先 `git pull --rebase origin main` |
| `Permission denied (publickey)` | SSH 公钥没配好或用错账号，先跑 `ssh -T git@github.com` |
| `Support for password authentication was removed` | 用了密码，改 token 或 SSH |
| 推上去发现少了文件 | `.gitignore` 把不该忽略的忽略了，`git check-ignore -v 文件` 查是哪条规则 |

还有一种更麻烦的情况：远端已经有内容。除了建仓库时勾了那三个文件，也可能是你本地这个目录以前接过别的远端，或者整个是从别的仓库拷来的，`.git` 里带着一堆旧提交。动手之前先看清楚：

```bash
git log --oneline | head     # 本地有哪些提交，commit message 是不是你要推的
git remote -v                # 现在接的是哪个远端
git ls-remote origin         # 远端上有哪些分支（能连上的话）
```

只有在你**确定远端那个仓库是你刚建的空仓库、里面的东西全都不要**的时候，才可以覆盖它：

```bash
git push -u origin main --force
```

这行会**把远端分支上的提交全部丢掉**，没有回收站，也没法从 GitHub 上恢复。别人的仓库、已经有人提过 PR 的仓库、已经有 star 的仓库，一律不要 force push。真遇到推不上去又不想丢东西的，宁可本地新建一个分支先推上去，慢慢理清了再合并。

---

## 5. 提交信息怎么写

`git commit -m "update"` 这种写法，三个月后你自己都看不出改了什么。够用的格式就一层：

```
<类型>: <一句话说改了什么>

（空行，可选）为什么这么改，有什么副作用
```

类型用这几个就够了：`feat` 新功能、`fix` 修 bug、`docs` 文档、`refactor` 重构、`test` 测试、`chore` 杂活（升级依赖、改构建配置）。中文没问题，本仓库的提交信息就是中文的。

比格式更重要的一条：**一个提交只做一件事**。理由很实在：

- 要回退的时候能只退那一个，不用连坐。
- `git bisect` 找 bug 靠逐个提交试，混杂的提交会让它指到一大片改动上。
- review 的人看十个各做一件事的提交，比看一个改了 40 个文件、混了 6 件事的提交轻松太多。
- 提交信息里那半句「为什么」，配上当时的上下文，是留给未来的自己唯一的线索。

对比一下：

```bash
# 差：三件事挤一起，回退任何一个都牵连其它
git commit -m "改了些东西"

# 好：每行独立，回退和追溯都干净
git commit -m "fix: 超时重试次数从 3 提到 5（第 3 次常因冷启动失败）"
git commit -m "docs: README 补上镜像源说明"
```

已经提交了想拆开：`git reset --soft HEAD~3` 退回暂存区重新分次提交。只在没 push 过的时候用，推上去的改写会影响别人。

---

## 6. 元文件

这些文件决定「陌生人第一次打开你仓库看到什么」。判断标准很朴素：一个不认识你的人，能不能在不问你一句话的情况下，搞懂这是什么、怎么跑起来、出问题找谁。

### README

放仓库根，GitHub 自动渲染到首页。至少要有这几段：

1. 一句话说这是什么。别只写「一个基于 XXX 的 YYY」，加一句它跟同类有什么不一样。
2. 现在的状态：能用 / 实验性 / 已停更。
3. 快速开始：装 + 跑起来的最短路径，命令要能直接复制粘贴。
4. 配置：要改哪些字段。密钥类只写环境变量名，别写真值。
5. 出问题去哪问（Issues 链接、有没有群、文档在哪）。
6. 许可。

可选但有用：截图或架构图（图放 `docs/` 或 `assets/`，用相对路径引）、徽章（CI 状态、许可、支持的语言版本）、一份翻译（比如 `README.zh-CN.md`，两个文件里互相链接）。

写 README 最常见的毛病是漏掉自己脑内的前提：写着「先装好 Python」却不写几版本以上，写着「配好数据库」却不写怎么配。有个笨办法很有效：找一台没装过你这东西的机器，或者起个干净的 Docker 容器，照着 README 一步步走，卡在哪就把哪补上。

骨架大概长这样，照着填就行：

```markdown
# 项目名

一句话说清这是什么，以及跟同类比有什么不一样。

![test](https://github.com/<你>/<仓库名>/actions/workflows/test.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-blue)

状态：能用 / 实验性 / 已停止维护。

## 安装

（三步以内，命令能直接复制粘贴）

## 快速开始

（跑起来的最短路径，最好附上正常情况的输出）

## 配置

| 字段 | 说明 |
| --- | --- |
| `API_KEY` | 环境变量，去 xxx 平台申请，不要写进配置文件 |

## 常见问题

- 报错 xxx：原因是 xxx，这样改 xxx。

## 参与

欢迎提 issue 和 PR，先看 CONTRIBUTING.md。
安全问题别开公开 issue，渠道见 SECURITY.md。

## 许可

MIT，见 LICENSE。
```

徽章不用手搓。在 Actions 里点进某个 workflow，界面上有生成状态徽章的入口，复制出来的 markdown 直接粘进 README（不同版本的界面位置不太一样，以你看到的为准）。许可、语言版本这类徽章用 shields.io 拼，格式不难。

README 写中文还是英文，看你希望谁来用。想让国际用户也能装得上，英文主文档加一份中文翻译，两份互相链接；纯粹面向国内的工具，中文直接写完全没问题。别为了显得国际化塞一堆机翻英文，那比纯中文劝退。

### LICENSE

不写 LICENSE 的仓库，法律上默认是「保留所有权利」，别人连拿去用都心虚。要开源就得明确选一个。

| 许可 | 大致意思 | 适合 |
| --- | --- | --- |
| MIT | 随便用，保留版权声明，作者不担责 | 大部分小项目、库、脚本 |
| Apache-2.0 | 跟 MIT 接近，多了明确的专利授权条款 | 公司背景、可能牵扯专利的项目 |
| GPL-3.0 | 衍生作品也必须开源 | 你确实在意别人闭源拿去卖 |
| AGPL-3.0 | 跑成网络服务也算分发，要开源 | 服务端软件 |
| BSD-3 / ISC | 与 MIT 接近 | 看你喜好 |

拿不准就 MIT，简单、兼容性好、用的人最多。去 [choosealicense.com](https://choosealicense.com/) 对着看也行。文件内容照抄官方模板，别自己改写条款。

两点提醒：

- 你**没有**版权的东西不能顺着你的许可证发出去：第三方的代码、别人授权的素材、带水印的图。有这种情况单独写一份来源与致谢说明（本仓库那种叫 [../NOTICE.md](../NOTICE.md)），并且确认原许可允许你再分发。
- 调用第三方 API、逆向别人接口这类事，许可证管不着，得单独写免责声明。免责声明不等于合法，别把它当挡箭牌，但至少让用的人知道风险。

### 社区文件

| 文件 | 解决什么问题 |
| --- | --- |
| `CONTRIBUTING.md` | 别人想改你的代码先看这个：怎么搭环境、跑哪些测试、什么风格的 PR 会被合 |
| `CODE_OF_CONDUCT.md` | 有人骂人、骚扰时你有依据处理。抄 Contributor Covenant 就行，别自己写 |
| `SECURITY.md` | 让人别在公开 issue 里贴漏洞和密钥，并写清私密上报渠道 |

位置：`CONTRIBUTING.md` 和 `SECURITY.md` 放仓库根或 `.github/` 下，GitHub 会自动把这些文件挂到该出现的地方（提 issue 时、首页侧栏）。本仓库的 [../CONTRIBUTING.md](../CONTRIBUTING.md)、[../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)、[../SECURITY.md](../SECURITY.md) 可以直接当模板抄。

### Issue 模板与 PR 模板

模板的作用是让报 bug 的人**一次把你诊断需要的信息给全**，否则就是来回问三轮「你什么系统」「什么版本」。

- `.github/ISSUE_TEMPLATE/bug_report.yml`：要版本、系统、复现步骤、日志。
- `.github/ISSUE_TEMPLATE/feature_request.yml`：问「想解决什么问题」，而不是「给我加个 X」。后者会把需求当成方案提，你没法讨论。
- `.github/PULL_REQUEST_TEMPLATE.md`：PR 自查清单（跑过测试没、有没有夹带密钥、关联哪个 issue）。

本仓库这几份都在，可以照着抄：[../.github/ISSUE_TEMPLATE/bug_report.yml](../.github/ISSUE_TEMPLATE/bug_report.yml)、[../.github/ISSUE_TEMPLATE/feature_request.yml](../.github/ISSUE_TEMPLATE/feature_request.yml)、[../.github/PULL_REQUEST_TEMPLATE.md](../.github/PULL_REQUEST_TEMPLATE.md)。

模板里最该写的一条：**贴日志和截图前把密钥、真实账号、聊天记录抹掉**。issue 是公开的，有人就是会手滑。

---

## 7. CI：GitHub Actions

CI 干的事很简单：每次 push 或提 PR，让机器在一台干净机器上把你的测试跑一遍。它挡的是「在我电脑上是好的」这类问题。

最小可用的 workflow 放 `.github/workflows/` 下，文件名随便，后缀 `.yml`：

```yaml
name: test

on:
  push:
    branches: [main]     # 推到 main 时跑
  pull_request:          # 有人提 PR 时也跑，这个触发器必须有
  workflow_dispatch:     # 加了这行，你能在网页上手动点一次 Run workflow

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4         # 把仓库拉进跑任务的机器，第一步永远是它
      - uses: actions/setup-python@v5     # 装指定版本的语言运行时
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python -m pytest -q          # 换成你自己的测试命令
```

Node 项目把 `setup-python` 换成 `actions/setup-node@v4`，测试命令改成 `npm ci && npm run test`。就这么点东西。

几个实用点：

- **`pull_request` 触发器一定要有**，否则别人提的 PR 根本不跑 CI，你就少一道防线。
- 想跑多平台 / 多版本就用 `matrix`。本仓库的 [../.github/workflows/test.yml](../.github/workflows/test.yml) 是三个系统乘三个 Python 版本，外加一个 Node 的 job。只在一个系统上测，「换个系统就崩」这类问题永远发现不了。
- 慢的 job 用 `cache:` 省时间，`setup-node` 支持 `cache: npm`。
- 密钥别写进 workflow 文件，用仓库 Settings 里的 Secrets and variables → Actions 存，代码里读 `${{ secrets.名字 }}`。**外部 fork 提的 PR 拿不到你的 secrets**，这是 GitHub 的保护机制，别想办法绕开。

矩阵是这样加的：

```yaml
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false            # 一个平台挂了别中断其它平台，一次就能看全
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        python-version: ["3.11", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -r requirements.txt
      - run: python -m pytest -q
```

矩阵是乘法，上面这份会跑 3 × 2 = 6 个 job，每个都要占几分钟配额。跑太久就砍维度，但至少留一个跟你开发机不一样的环境。

写测试这件事在 CI 上比在本地敏感得多，几条经验：

- **测试要能离线跑。** 一旦依赖外网接口，CI 会三天两头红一次，红的次数多了就没人看了，等于没上 CI。
- **测试不要碰真实数据文件。** 把可写路径重定向到临时目录，跑完做一次校验。本地那种「我这儿有个状态文件所以能过」的测试，到了每次都是全新环境的 CI 上必挂。
- 别用 `sleep` 等真实超时，用 mock 把等待那层换掉，否则 CI 时间会全花在等待上。
- 装完依赖跑一次 `pip check`（Node 侧 `npm ls`），能抓出版本打架。这种问题在别人机器上通常表现为莫名其妙的崩溃，非常难查。

还有一种检查值得一学：验证「某条命令真的什么都没改」。做法是执行前后各打一次 `git status`，要求两边完全一致，不一致就判失败。它能挡住「脚本顺手改了工作区」这类问题，本仓库的 [../.github/workflows/install-dryrun.yml](../.github/workflows/install-dryrun.yml) 就是干这个的。

失败日志怎么读：

1. 仓库的 Actions 标签页 → 点进那次红的 → 点进失败的 job → 展开红色的步骤。
2. 从上往下找**第一条**报错。后面的多半是它的连锁反应（找不到文件 → 命令失败 → 后续步骤全挂），盯着最后一条看会跑偏。
3. 常见几类：本地能过 CI 不过，多半是路径大小写（Linux 区分大小写）、换行符、依赖没写进清单、环境变量没配；只有一个系统失败，基本是路径拼接或命令名差异（`python` 和 `python3`）；偶发的超时和网络失败，重跑一次，频繁出现就说明测试该改成离线。
4. 想在本地复现，最接近的办法是用同一个基础镜像起容器，版本号对上去。

本仓库已经有三个 workflow 了（各守什么见 [开源指南.md §3](开源指南.md)），如果你手上的仓库也有，这一节可以整段跳过。只有三行值得现在回去补：`workflow_dispatch`、`pull_request` 触发器、多平台矩阵。

---

## 8. 版本号与 Release

### 版本号

用 SemVer，写成 `主.次.补丁`：

- **主版本**（1 → 2）：有破坏性改动，别人升级要改自己的代码。
- **次版本**（1.4 → 1.5）：加了功能，向后兼容。
- **补丁**（1.4.1 → 1.4.2）：只修 bug。

`0.x` 阶段表示还没稳定，接口随时会变。发 `1.0.0` 的潜台词是「我承诺这套接口不乱改了」，所以别随手发。

### 打 tag

tag 是给某个提交起的固定名字，发布靠它。

```bash
git tag -a v1.4.2 -m "v1.4.2: 修 xxx 崩溃"    # -a 是带说明的注释 tag，发版别用轻量 tag
git push origin v1.4.2                        # tag 不会跟着普通 push 走，要单独推
```

tag 名习惯带 `v`，代码里的版本号不带，两边要一致，不然用户对不上号。推上去的 tag 别删，别人可能已经拉到了。

### Release

Release 是 tag 的说明页，也是别人下载东西的地方。网页上：Releases → Draft a new release → 选或新建 tag → 写标题和说明 → 把产物拖进 Assets → 发布。

说明里值得写的：

- 这一版加了什么、修了什么，用用户视角写，别直接贴 commit 列表。
- **破坏性改动单独拎出来**，写清用户要改什么地方。
- 安装 / 升级方法。
- 已知问题。

### 产物挂 Assets，不要提交进仓库

原因是这样：

- 二进制进 git 后，每发一版就往历史里塞几十 MB，几年后仓库 clone 不动。
- Assets 不受版本库影响，能删能换，还有下载计数。
- 用户找下载只会去 Releases 页面，没人会去仓库里翻 zip。

配套就是把构建脚本放进仓库，产物写进 `.gitignore`：

```bash
python tools/make_dist.py --version 1.4.2   # 产物留在 dist/，dist/*.zip 已在 .gitignore 里，别 git add -f
git tag -a v1.4.2 -m "v1.4.2"
git push origin v1.4.2
# 然后到网页建 Release，把 dist 里的包拖进 Assets
```

每次打包步骤都一样的话，可以写成 Actions：打 tag 时自动构建并上传，用 `gh release upload` 或现成的 action 都行。

### 校验和

分发安装包时顺手把 sha256 写进 Release 说明：

```bash
sha256sum dist/xxx.zip
# Windows PowerShell: Get-FileHash dist\xxx.zip -Algorithm SHA256
```

下载的人能验证包没被中间环节改过。更正式的做法是给 tag 做 GPG 签名，个人项目一般用不上。

---

## 9. 仓库设置

### 分支保护

Settings → Branches（新版把这些挪到了 Rules / Rulesets，以你看到的界面为准）→ 针对 `main` 加一条：

- Require a pull request before merging：哪怕你一个人开发也建议开，防止手滑 force push 把主干冲掉。
- Require status checks to pass：勾上你的 CI job，红了就合不了。
- 禁止 force push、禁止删除分支。
- 如果有 bypass 的选项，别给自己开，开了就一定会用。

配好之后，你自己改东西也得走「开分支 → 提 PR → CI 绿 → 合并」。麻烦一点，换来的是主干上永远是能跑的。

### About 和 Topics

首页 About 那块的齿轮：描述、网址（有文档站就填）、Topics。Topics 是可搜索的，把技术栈和用途写上。

### Gitee 镜像

国内实际情况是 GitHub 时通时不通，Gitee 访问稳定。两条路：

1. Gitee 自带的导入：Gitee 新建仓库时选从 GitHub 导入，填地址，之后在 Gitee 仓库页面点同步。缺点是每次都要手点。
2. 本地一次推两个远端：

```bash
git remote set-url --add --push origin https://github.com/<你>/<仓库名>.git
git remote set-url --add --push origin https://gitee.com/<你>/<仓库名>.git
git push origin main     # 一条命令推两个地方
```

第二条路的问题是两边任一挂着（比如 Gitee 要重新登录），push 就整个失败。用哪条看你自己推得频不频繁。

镜像记得在两边 README 里互相写一句，从 Gitee 搜到你的人得知道原仓在哪。

---

## 10. 以后怎么维护

### Issue

- 给 issue 打标签。哪怕只分 `bug` / `enhancement` / `question` 三类，你以后翻的时候都省事。重复的及时关掉，说清为什么。
- 信息不全的 bug 别猜着修，回一句「麻烦贴一下版本和日志」。Issue 模板就是为这个存在的。
- 有人在 issue 里贴了密钥：**先删掉那条内容**，再提醒他去吊销。只在回复里说「记得删」没用，内容已经公开了。
- 长期不打算做的功能别挂着，关了或者标 `wontfix`，附一句为什么。

### PR

收到 PR 看三件事：CI 绿不绿（红了先让对方自己看日志）、一个 PR 是不是只做一件事（混着来的请他拆开）、改动符不符合项目风格。

这里有一条要单独拎出来说：**改了安装脚本、构建脚本、CI 配置的 PR，逐行读。** 这类文件是能做坏事的地方，常见手法是在里面塞一句「下载一个文件然后执行」：

```bash
curl -fsSL https://example.com/setup.sh | bash
```

这种写法本身不罕见，很多正经工具的官方安装命令就长这样，所以不能一棍子打死。但你的仓库里不该出现指向你不控制的不明地址的下载执行，更不该有先解码再执行的东西：

```bash
echo "..." | base64 -d | bash     # PR 里有这个，先打问号再问人
```

想验证 PR 到底干了什么，别急着跑，先看差异：

```bash
gh pr checkout 123        # 有 gh CLI 的话拉下来
git diff main...HEAD      # 先读 diff，再决定要不要跑
```

合并方式三种：squash 把一堆小提交压成一个，适合乱糟糟的 PR；普通 merge commit 保留分支结构；rebase 不留合并节点。一个人开发推荐 squash，主干历史干净。

### 一次完整的响应长什么样

把上面这些串起来看一遍，你就知道维护一个仓库的日常节奏了：

1. 有人在 issue 里报「装完跑不起来」，模板让他贴了系统、版本和日志。
2. 你在自己机器上照着他的描述复现，确认了是某段路径拼接在 Windows 上不成立。
3. 开个分支修：

```bash
git switch -c fix/win-path          # 从 main 拉一个短命分支
# 改代码，补一个能覆盖这个情况的用例
git commit -m "fix: Windows 下路径拼接少了分隔符（issue #12）"
git push -u origin fix/win-path
# 网页上提 PR，等 CI 绿，合掉
```

4. 顺手把 README 里那句让人误解的说明改了，同一个 PR 里带上（它和这次修复是同一件事）。
5. CI 绿了合进 main，然后发补丁版：

```bash
git switch main && git pull
git tag -a v1.2.1 -m "v1.2.1: 修 Windows 路径拼接"
git push origin v1.2.1
# 网页上建 Release，说明里写「影响 1.2.0 及以前，升级到 1.2.1 即可」
```

6. 回到那个 issue，回一句「v1.2.1 已修，麻烦试试」，然后关掉。这一步别忘，报 bug 的人最在意的就是有没有人管。

整个过程里真正花时间的只有第 2 步和第 3 步，其它都是照流程走。这也是为什么前面反复强调元文件和 CI：它们的作用是把沟通和验证的成本压下来，让第 2、3 步之外的事不占你精力。

### 依赖更新

两种策略，各有各的代价：

- **下限（`>=`）**：写 `httpx>=0.27`，装的人拿到最新版。好处是不会因为某个老版本在新的语言运行时上没预编译包而装不上；坏处是上游发了个不兼容的小版本，你的项目今天突然就坏了，而你什么都没改。
- **锁死（`==`）**：写 `httpx==0.27.2`。好处是可复现，今天装和一年后装结果一样；坏处是安全补丁来了要手动升，而且老版本在新运行时上常常没有预编译包，pip 退回源码编译，用户机器上没有编译器就直接失败。带 C 扩展的包尤其明显。

常见的折中：主清单写下限保灵活，另出一份 lock 给需要完全复现的场景（`pip freeze > requirements.lock.txt`、`package-lock.json`）。本仓库就是这么做的，理由在 [../CONTRIBUTING.md](../CONTRIBUTING.md) 的依赖那一节里写得很细。

自动化方面，GitHub 自带 Dependabot，能在 Settings 的 security 相关页面开启，定期提升级 PR。开了就别放着不管，该合合该关关，堆一堆红的 PR 反而让人养成不看的习惯。

### 文档

代码改了文档没改，比没有文档更坏，因为它在骗人。截图和命令尤其容易过期，改界面时顺手改。有个习惯值得养成：每次发版前照 README 的快速开始从头做一遍。

---

## 11. 国内网络

### git 慢

```bash
# 走本地代理（端口按你实际用的改）
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890

# 取消
git config --global --unset http.proxy
git config --global --unset https.proxy
```

记得把本地地址排除在代理外，否则连本机的服务会失败。这两个是环境变量，不是 git 配置：

```bash
# Linux / macOS
export NO_PROXY=127.0.0.1,localhost

# Windows PowerShell
$env:NO_PROXY = "127.0.0.1,localhost"
```

SSH 走代理要单独配 `~/.ssh/config`，写法取决于你装了什么工具（`connect`、`ncat` 之类），不确定就先只给 HTTPS 配代理。另一个思路是让 SSH 走 443 端口，GitHub 官方支持 `ssh.github.com` 的 443 端口，有些网络里 22 被挡但 443 是通的。

### 包源

```bash
# pip：全局切清华源
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 或者只这次用
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# npm
npm config set registry https://registry.npmmirror.com

# 装一半失败，先清缓存重试
pip install --no-cache-dir -r requirements.txt
npm cache clean --force
```

这些命令光你自己用不够，写进 README 的「国内加速」小节，能让一半的人少折腾半小时。项目级配置放 `.npmrc` / `pip.conf` 也行，但别把某个镜像源写死进代码里：镜像源会下线，写死之后所有人都受影响。

---

## 12. 一页速查

```bash
# ① 身份（提交邮箱用 noreply，别暴露真实邮箱）
git config --global user.name "Your Name"
git config --global user.email "123456789+你的用户名@users.noreply.github.com"

# ② 推之前检查：密钥、本机路径、不该跟踪的文件
git status --ignored --short
grep -rInE "(api[_-]?key|access_token|secret)\s*[:=]\s*[\"'][^\"']{16,}" . | grep -v node_modules

# ③ 首次提交与推送
git add -A && git status
git commit -m "feat: 首个公开版本"
git branch -M main
git remote add origin git@github.com:<你>/<仓库名>.git
git push -u origin main

# ④ 网页上：填描述和 Topics、开分支保护、确认 Issues 是开的
# ⑤ 本地补齐 LICENSE / README / .gitignore / .gitattributes（别在网页上加）
# ⑥ 加一个最小 CI（.github/workflows/test.yml）
# ⑦ 发版
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
# 再到网页建 Release，把打包产物拖进 Assets

# ⑧ 可选：Gitee 镜像 + README 里加国内加速说明
```

完成标志就一句话：一个不认识你的人，只靠 README，能在半小时内把它跑起来。跑不通就说明文档还有缺口，这是最省事的验收标准。

本仓库的具体清单（安装脚本、三个 workflow、密钥与 QQ 号那件事的收尾）在 [开源指南.md](开源指南.md)。
