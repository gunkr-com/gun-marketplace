# harnesses

**[English](#english)** · **[한국어](#한국어)**

---

## English

Harnesses for Claude Code that live in their own repositories. This folder holds only their documentation: the catalog entry installs each one straight from its own repository, and no harness code is copied here.

| Plugin | Status | What it does | Source |
| :- | :- | :- | :- |
| `cab-of-one` | Preview | An interview-led generator that writes change control for IT operations into a Claude Code project as hooks and deny rules; a person applies the result from their own terminal. Snapshots, verification and allowed writes use only the simulated tenant; real-tenant writes are refused, classified real-tenant reads remain allowed | [`gunkr-com/cab-of-one`](https://github.com/gunkr-com/cab-of-one) (Apache-2.0) |

### cab-of-one

A preview of an interview-led change-control generator for Claude Code in IT operations projects. Its setup skill asks you about your project and renders hooks, deny rules, subagents and skills for it into a staging folder. Nothing takes effect until a person applies the result from their own terminal. What the preview does not do:

- **Writes go to the simulator only.** Snapshots, verification and allowed writes use only the simulated tenant. Writes to a real tenant are refused. Classified reads from a real tenant remain allowed.
- **Claude Code only.** Other agents, Codex included, are not supported, and this marketplace has no Codex entry for it.
- **Not stable.** Behaviour and options may change between versions.

The source code and its README are in [`gunkr-com/cab-of-one`](https://github.com/gunkr-com/cab-of-one), under the Apache-2.0 license. This marketplace's MIT license does not cover it.

#### Requirements

From the cab-of-one README: Python 3.11 or later, Git, and Claude Code 2.1.295 or later, on macOS or Linux.

#### Install

A plugin is code that runs with your permissions. Read the [source](https://github.com/gunkr-com/cab-of-one) before you install.

```bash
claude plugin marketplace add gunkr-com/gun-marketplace
claude plugin install cab-of-one@gun-marketplace
```

In a Claude Code session the same commands are `/plugin marketplace add gunkr-com/gun-marketplace` and `/plugin install cab-of-one@gun-marketplace`. Claude Code downloads the plugin from `gunkr-com/cab-of-one`, not from this repository. It loads the next time you start a session; in a session that is already open, run `/reload-plugins`.

Installing the plugin fetches its setup skill and source; on its own it applies no controls to a project, though it registers a hook that guards setup sessions. Applying the generated controls to a project is a separate step that a person runs in their own terminal (`cab setup apply`). Installing the plugin is not a test of the full setup: the complete conversational setup interview has not been measured. The [cab-of-one README](https://github.com/gunkr-com/cab-of-one#readme) describes setup, apply and a demo in detail.

To remove it:

```bash
claude plugin uninstall cab-of-one@gun-marketplace
```

---

## 한국어

자체 저장소에 있는 Claude Code 하네스를 모은 분류입니다. 이 폴더에는 문서만 있습니다. 카탈로그 항목이 각 하네스를 그 저장소에서 바로 설치하며, 하네스 코드를 여기로 복사하지 않습니다.

| 플러그인 | 상태 | 하는 일 | 소스 |
| :- | :- | :- | :- |
| `cab-of-one` | 미리보기 | 인터뷰로 IT 운영용 변경 통제(change control)를 Claude Code 프로젝트의 훅과 deny 규칙으로 생성함. 결과는 사람이 자기 터미널에서 직접 적용함. 스냅샷·검증·허용된 쓰기는 시뮬레이션 테넌트에서만 하고 실제 테넌트 쓰기는 거부함. 분류된 실제 테넌트 읽기는 허용됨 | [`gunkr-com/cab-of-one`](https://github.com/gunkr-com/cab-of-one) (Apache-2.0) |

### cab-of-one

IT 운영 프로젝트에서 쓰는 Claude Code용 변경 통제를 인터뷰로 생성하는 도구의 미리보기입니다. 설정 스킬이 프로젝트에 대해 묻고, 훅·deny 규칙·서브에이전트·스킬을 프로젝트 안의 준비(staging) 폴더에 렌더링합니다. 사람이 자기 터미널에서 결과를 적용하기 전에는 아무것도 적용되지 않습니다. 미리보기에서 하지 않는 것:

- **쓰기는 시뮬레이터에만.** 스냅샷, 검증, 허용된 쓰기는 시뮬레이션 테넌트에서만 합니다. 실제 테넌트에 쓰는 요청은 거부합니다. 분류된 실제 테넌트 읽기는 허용됩니다.
- **Claude Code 전용.** Codex를 비롯한 다른 에이전트는 지원하지 않으며, 이 마켓플레이스에 Codex용 항목도 없습니다.
- **안정판 아님.** 버전 사이에 동작과 옵션이 바뀔 수 있습니다.

소스 코드와 README는 [`gunkr-com/cab-of-one`](https://github.com/gunkr-com/cab-of-one)에 있고 Apache-2.0 라이선스를 따릅니다. 이 마켓플레이스의 MIT 라이선스는 적용되지 않습니다.

#### 요구 사항

cab-of-one README 기준: macOS 또는 Linux에서 Python 3.11 이상, Git, Claude Code 2.1.295 이상.

#### 설치

플러그인은 내 권한으로 실행되는 코드입니다. 설치하기 전에 [소스](https://github.com/gunkr-com/cab-of-one)를 읽어 보세요.

```bash
claude plugin marketplace add gunkr-com/gun-marketplace
claude plugin install cab-of-one@gun-marketplace
```

Claude Code 세션 안에서는 같은 명령을 `/plugin marketplace add gunkr-com/gun-marketplace`, `/plugin install cab-of-one@gun-marketplace`로 입력합니다. Claude Code는 이 저장소가 아니라 `gunkr-com/cab-of-one`에서 플러그인을 내려받습니다. 다음에 세션을 시작할 때 로드되며, 이미 열린 세션에서는 `/reload-plugins`를 실행합니다.

플러그인을 설치하면 설정 스킬과 소스를 받아 옵니다. 설치만으로는 프로젝트에 통제가 적용되지 않으며, 설정 세션을 지키는 hook이 하나 등록됩니다. 생성된 통제를 프로젝트에 적용하는 것은 사람이 자기 터미널에서 따로 실행하는 단계(`cab setup apply`)입니다. 플러그인 설치가 전체 설정을 검증하지는 않으며, 완전한 대화형 설정 인터뷰는 측정되지 않았습니다. 설정, 적용, 데모는 [cab-of-one README](https://github.com/gunkr-com/cab-of-one#readme)에 자세히 있습니다.

제거하려면:

```bash
claude plugin uninstall cab-of-one@gun-marketplace
```
