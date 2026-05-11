"""
CC命令执行器（依次执行 command1 ~ command4）

功能概述
--------
读取 input_dir 下的任务目录（由 4.2 生成），拷贝到 run_dir 下，顺次执行 4 条命令，
支持断点续执行，完成后按需收集结果并执行 verify.py 校验。

工作目录结构
--------
run_dir/task_N/
├── workdir1/   # command1, command2 的执行目录
│   ├── command1.txt ~ command4.txt   # 各阶段 prompt（统一从 workdir1 读取）
│   ├── task.txt                      # command1 生成
│   ├── meta.json, resource_paths.json  # command2 生成
│   └── *.{xlsx,pptx,docx,eml,...}   # command2 生成的初始文件
├── workdir2/   # command3, command4 的执行目录
│   ├── task.txt, resource 文件        # command3 前从 workdir1 拷贝
│   ├── deliverable_paths.json         # command3 生成（参考交付件）
│   ├── rubrics.txt, verify.py         # command4 生成（评分细则）
│   └── *.{xlsx,pptx,docx,eml,...}   # command3 生成的参考交付件
└── state.json   # 记录已完成的命令，支持断点续执行

命令执行流程
--------
command1 -> workdir1   生成 task.txt
command2 -> workdir1   生成文件大纲（file_outlines.json）和高保真内容（file_content.json），并保存 resource_paths.json
command3 -> workdir2   生成参考交付件，写入 deliverable_paths.json（command3 执行前自动从 workdir1 拷贝 task.txt 和 resource 文件）
command4 -> workdir2   生成评分细则 rubrics.txt 和自动化校验脚本 verify.py

轨迹文件
--------
每条命令的轨迹写入 commandi_traj.txt（文本）和 commandi_traj.jsonl（JSONL）

校验与收集
--------
- 每个命令执行完毕后立即本地校验（verify_command1~4），失败则中止该任务
- 使用 --collect_and_verify 时，最终执行 verify.py 并按结果过滤（删除返回 -1/2 的任务）
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import query as claude_query
from claude_agent_sdk.types import (
    AssistantMessage,
    ContentBlock,
    Message,
    UserMessage,
)

COMMANDS = ["command1", "command2", "command3", "command4"]


# ============================================================================
# 工具函数
# ============================================================================

def load_json(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(data, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def serialize_block(block: ContentBlock) -> dict[str, Any]:
    data = asdict(block)
    data["_type"] = block.__class__.__name__
    return data


def serialize_message(message: Message) -> dict[str, Any]:
    data = asdict(message)
    data["_type"] = message.__class__.__name__

    if isinstance(message, (UserMessage, AssistantMessage)):
        if isinstance(message.content, list):
            data["content"] = [serialize_block(block) for block in message.content]

    return data


# ============================================================================
# 检验函数
# ============================================================================

def verify_command1(workdir) -> bool:
    """命令1：检查 task.txt 是否生成"""
    flag = os.path.exists(os.path.join(workdir, 'task.txt'))
    return flag, ('task.txt exists' if flag else 'task.txt not exists')


def verify_command2(workdir) -> bool:
    """命令2：从 meta.json 提取输入文件路径，保存到 resource_paths.json，然后检查是否全部存在"""
    meta_file = os.path.join(workdir, 'meta.json')
    rp_file = os.path.join(workdir, 'resource_paths.json')

    if not os.path.exists(meta_file):
        return False, "meta.json not found"

    try:
        meta = load_json(meta_file)
        material_design = meta.get('material_design', {})
        file_info = material_design.get('file_info', [])

        # 提取所有文件路径
        paths = []
        for fi in file_info:
            path = fi.get('path')
            paths.append(path)

        # 保存到 resource_paths.json
        save_json(paths, rp_file)

        # 检查所有文件是否都存在
        missing = [p for p in paths if not os.path.exists(os.path.join(workdir, p))]
        all_exist = len(missing) == 0
        details = f"resource_paths.json saved with {len(paths)} paths, all_exist={all_exist}"
        if not all_exist:
            details += f", missing: {missing}"
        return all_exist, details
    except Exception as e:
        return False, f"error: {e}"


def verify_command3(workdir) -> bool:
    """命令3：检查 deliverable_paths.json 是否生成，且其中路径都存在"""
    dp_file = os.path.join(workdir, 'deliverable_paths.json')
    if not os.path.exists(dp_file):
        return False, "deliverable_paths.json not exists"
    try:
        paths = load_json(dp_file)
        # paths 应为字符串列表
        if not isinstance(paths, list) or len(paths) == 0:
            return False, "deliverable_paths.json is empty or invalid"
        all_exist = all(os.path.exists(os.path.join(workdir, p)) for p in paths)
        details = f"deliverable_paths.json has {len(paths)} paths, all_exist={all_exist}"
        return all_exist, details
    except Exception:
        return False, "deliverable_paths.json parse error"


def verify_command4(workdir) -> bool:
    """命令5：检查 rubrics.txt, verify.py 是否生成"""
    flag = os.path.exists(os.path.join(workdir, 'rubrics.txt'))
    if not flag:
        return False, 'rubrics.txt not exists'

    flag = os.path.exists(os.path.join(workdir, 'verify.py'))
    if not flag:
        return False, 'verify.py not exists'

    return True, 'rubrics.txt, verify.py exist'




VERIFY_FUNCS = {
    "command1": lambda d: verify_command1(d),
    "command2": lambda d: verify_command2(d),
    "command3": lambda d: verify_command3(d),
    "command4": lambda d: verify_command4(d),
}


# ============================================================================
# 单条命令的执行
# ============================================================================

async def exec_command(workdir, prompt, command_name):
    """执行单条命令，将轨迹写入 commandi_traj.txt 和 commandi_traj.jsonl"""
    os.makedirs(workdir, exist_ok=True)

    options = ClaudeAgentOptions(
        cwd=workdir,
        setting_sources=["project"],
        allowed_tools=[
            "Skill", "Read", "Write", "Bash", "Task", "TaskOutput",
            "Glob", "EnterPlanMode", "ExitPlanMode", "Edit", "TodoWrite", "Grep",
        ],
        env={"MAX_THINKING_TOKENS": "100000"},
        max_turns=200,
        permission_mode="bypassPermissions",
    )

    traj_txt = os.path.join(workdir, f"{command_name}_traj.txt")
    traj_jsonl = os.path.join(workdir, f"{command_name}_traj.jsonl")

    # 如果已有轨迹文件，避免直接覆盖，重命名备份
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    for fp in [traj_txt, traj_jsonl]:
        if os.path.exists(fp):
            backup = os.path.join(os.path.dirname(fp), f"{ts}_{os.path.basename(fp)}")
            os.rename(fp, backup)

    # 清空轨迹文件
    for fp in [traj_txt, traj_jsonl]:
        open(fp, 'w', encoding='utf-8').close()

    prompt = f"在进行文件读写的时候，请谨记当前目录是 `{workdir}`。\n\n" + prompt
    async for message in claude_query(prompt=prompt, options=options):
        try:
            with open(traj_txt, 'a', encoding='utf-8') as fp:
                fp.write(str(message).encode('utf-8', errors='replace').decode('utf-8') + '\n')
            with open(traj_jsonl, 'a', encoding='utf-8') as fp:
                fp.write(json.dumps(serialize_message(message), ensure_ascii=False) + '\n')
        except UnicodeEncodeError:
            print(str(message).encode('utf-8', errors='replace').decode('utf-8'))


# ============================================================================
# 拷贝skills
# ============================================================================


# def copy_skills(dst_dir):
#     _dst_dir = os.path.join(dst_dir, '.claude', 'skills')
#     os.makedirs(os.path.join(dst_dir, '.claude'), exist_ok=True)
#     shutil.copytree(SKILL_DIRS, _dst_dir, dirs_exist_ok=True)


import sys
from pathlib import Path
def copy_skills(dst_dir: str) -> bool:
    """
    用软链接替代之前的物理复制，将中央 skills 库映射到目标目录的 .claude/skills
    """
    # 1. 确保源目录存在并获取绝对路径（软链接最好使用绝对路径，避免由于运行目录变化导致链接失效）
    target_dir = Path(SKILL_DIRS).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"❌ 源 Skills 目录不存在或不是文件夹: {target_dir}")
        raise Exception('error while copying skills')

    # 2. 准备目标的 .claude/skills 路径
    local_claude_dir = Path(dst_dir) / ".claude"
    local_skills_link = local_claude_dir / "skills"

    # 创建目标环境的 .claude 目录
    local_claude_dir.mkdir(parents=True, exist_ok=True)

    # 3. 冲突检测与清理（针对你之前用 copytree 留下的历史实体文件夹）
    if local_skills_link.exists() or local_skills_link.is_symlink():
        if local_skills_link.is_symlink():
            # 如果已经是软链接，且指向正确，直接返回成功
            if local_skills_link.resolve() == target_dir:
                return True
            else:
                # 指向了错误的地方，删掉重建
                local_skills_link.unlink()
        else:
            # ⚠️ 关键逻辑：发现以前 copy 留下的真实文件夹，将其彻底删除以便腾出位置建软链接
            print(f"🧹 清理旧的实体 Skills 文件夹: {local_skills_link}")
            shutil.rmtree(local_skills_link)

    # 4. 创建软链接
    try:
        local_skills_link.symlink_to(target_dir, target_is_directory=True)
        # print(f"🔗 成功创建软链接: {local_skills_link} -> {target_dir}")
        return True
    except OSError as e:
        # 5. 兼容 Windows 无管理员权限的情况
        if sys.platform == "win32" and getattr(e, 'winerror', 0) == 1314:
            return _create_windows_junction(target_dir, local_skills_link)
            
        print(f"❌ 创建软链接失败: {e}")
        raise Exception('error while copying skills')

def _create_windows_junction(target_dir: Path, link_path: Path) -> bool:
    """Windows 下非管理员权限的目录联接回退方案"""
    import subprocess
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return True
    except subprocess.CalledProcessError as err:
        print(f"❌ 目录联接创建失败: {err.stderr.decode('gbk', errors='ignore')}")
        raise Exception('error while copying skills')


# ============================================================================
# 拷贝task.txt和resource文件
# ============================================================================

def copy_task_and_resources(src_dir, dst_dir):
    """只拷贝 task.txt 和 resource 文件"""
    os.makedirs(dst_dir, exist_ok=True)
    # 拷贝 task.txt
    src_task = os.path.join(src_dir, 'task.txt')
    if os.path.exists(src_task):
        shutil.copy(src_task, os.path.join(dst_dir, 'task.txt'))
    # 拷贝 resource_paths.json 中的文件
    
    rp_file = os.path.join(src_dir, 'resource_paths.json')
    if os.path.exists(rp_file):
        resource_paths = load_json(rp_file)
        for resource_path in resource_paths:
            src_path = os.path.join(src_dir, resource_path)
            if os.path.exists(src_path):
                dst_path = os.path.join(dst_dir, resource_path)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                shutil.copy(src_path, dst_path)


# ============================================================================
# 单个任务的执行逻辑
# ============================================================================

async def exec_task_logic(src_dir, run_dir, i):
    """
    对单个任务目录，拷贝到 run_dir/workdir1，然后依次执行 command1 ~ command6，支持断点续执行。
    每个命令在不同的 cwd 目录下执行：
      command1, command2 -> run_dir/workdir1
      command3, command4 -> run_dir/workdir2
      command5, command6 -> run_dir/workdir3
    command 文件统一从 workdir1 读取。
    """
    task_name = os.path.basename(src_dir)
    print(f"Starting Task {i}: {task_name}")

    # 工作目录定义
    workdir1 = os.path.join(run_dir, 'workdir1')  # command1, command2 的工作目录
    workdir2 = os.path.join(run_dir, 'workdir2')  # command3, command4 的工作目录
    # workdir3 = os.path.join(run_dir, 'workdir3')  # command5, command6 的工作目录

    def get_cwd(cmd_name):
        if cmd_name in ('command1', 'command2'):
            return workdir1
        elif cmd_name in ('command3', 'command4'):
            return workdir2
        # elif cmd_name in ('command5', 'command6'):
        #     return workdir3
        return run_dir

    # 1. 拷贝任务到执行目录（如果尚未拷贝）
    if not os.path.exists(workdir1):
        os.makedirs(os.path.dirname(run_dir), exist_ok=True)
        shutil.copytree(src_dir, workdir1, dirs_exist_ok=True)
        print(f"\tCopied {src_dir} -> {workdir1}")
        copy_skills(workdir1)
        print(f"\tCopied skills -> {workdir1}")

    # 2. 加载或初始化 state.json（放在 run_dir 下）
    state_file = os.path.join(run_dir, 'state.json')
    if os.path.exists(state_file):
        try:
            state = load_json(state_file)
        except Exception:
            state = []
    else:
        state = []

    # 3. 依次执行命令
    for cmd_name in COMMANDS:
        # 断点续执行：跳过已完成的命令
        if cmd_name in state:
            print(f"\t{cmd_name} already completed, skipping.")
            continue

        # 读取命令内容（从 workdir1 读取）
        cmd_file = os.path.join(workdir1, f"{cmd_name}.txt")
        if not os.path.exists(cmd_file):
            print(f"\t{cmd_file} not found, skipping.")
            continue

        with open(cmd_file, 'r', encoding='utf-8') as f:
            prompt = f.read().strip()

        assert prompt != ''

        # command3 执行前，拷贝 task.txt 和 resource 文件到 workdir2
        if cmd_name == 'command3':
            if not os.path.exists(workdir2):
                copy_task_and_resources(workdir1, workdir2)
                copy_skills(workdir2)
                print(f"\tCopied minimal files workdir1 -> workdir2")

        # 执行命令（使用对应的 cwd）
        cwd = get_cwd(cmd_name)
        await exec_command(workdir=cwd, prompt=prompt, command_name=cmd_name)

        # 检验执行结果
        verify_fn = VERIFY_FUNCS[cmd_name]
        passed, details = verify_fn(cwd)
        if not passed:
            print(f"\t{cmd_name} verification FAIL ({details}), aborting task. ({cwd})")
            return
        print(f"\t{cmd_name} verification PASS ({details}). ({cwd})")

        # 更新 state.json
        state.append(cmd_name)
        save_json(state, state_file)


# ============================================================================
# 并发调度
# ============================================================================

async def run_tasks(task_pairs, max_workers, task_timeout=600):
    """task_pairs: list of (src_dir, run_dir)"""
    semaphore = asyncio.Semaphore(max_workers)

    async def run_with_timeout(src_dir, run_dir, i):
        async with semaphore:
            try:
                async with asyncio.timeout(task_timeout):
                    await exec_task_logic(src_dir, run_dir, i)
            except TimeoutError:
                print(f"Task {i} ({os.path.basename(src_dir)}) timed out (>{task_timeout}s)")
            except Exception as e:
                print(f"Task {i} ({os.path.basename(src_dir)}) error: {e}")

    async_tasks = [
        run_with_timeout(src_dir, run_dir, i)
        for i, (src_dir, run_dir, _) in enumerate(task_pairs)
    ]
    await asyncio.gather(*async_tasks, return_exceptions=True)


# ============================================================================
# 环境配置
# ============================================================================

def set_env(model):
    if model == 'MiniMax-M2.7':
        os.environ['ANTHROPIC_AUTH_TOKEN'] = "sk-iMFpIpVG4vXPt5jFDO9mHDvu5uOTZ8sHbjusQtYMUR1Xt6VB"
        os.environ['ANTHROPIC_BASE_URL'] = "https://yibuapi.com"
        os.environ['ANTHROPIC_MODEL'] = "MiniMax-M2.7"
        os.environ["https_proxy"] = "http://10.70.85.53:9090"
        os.environ["CLAUDE_API_VERIFY_SSL"] = "false"
        os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    elif model == 'glm-5.1-thinking':
        os.environ['ANTHROPIC_AUTH_TOKEN'] = "sk-iMFpIpVG4vXPt5jFDO9mHDvu5uOTZ8sHbjusQtYMUR1Xt6VB"
        os.environ['ANTHROPIC_BASE_URL'] = "https://yibuapi.com"
        os.environ['ANTHROPIC_MODEL'] = "glm-5.1-thinking"
        os.environ["https_proxy"] = "http://10.70.85.53:9090"
        os.environ["CLAUDE_API_VERIFY_SSL"] = "false"
        os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    elif model == 'claude-opus-4-6-thinking':
        os.environ['ANTHROPIC_AUTH_TOKEN'] = "sk-iMFpIpVG4vXPt5jFDO9mHDvu5uOTZ8sHbjusQtYMUR1Xt6VB"
        os.environ['ANTHROPIC_BASE_URL'] = "https://yibuapi.com"
        os.environ['ANTHROPIC_MODEL'] = "claude-opus-4-6-thinking"
        os.environ["https_proxy"] = "http://10.70.85.53:9090"
        os.environ["CLAUDE_API_VERIFY_SSL"] = "false"
        os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"

    else:
        raise NotImplementedError(f"Unknown model: {model}")
    

# ============================================================================
# 检验verify的正确性
# ============================================================================


def exec_verify(workdir):
    """执行 verify.py 并返回状态码:
    -1: 执行失败
     0: 执行成功，打印全为True的列表，允许包含None
     1: 执行成功，打印列表包含False
     2: 执行成功，其他情况
    """
    verify_py = os.path.join(workdir, 'verify.py')
    if not os.path.exists(verify_py):
        print(f"verify.py not found in {workdir}")
        return -1

    try:
        result = subprocess.run(
            ['python', verify_py],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=300
        )
        print(f"verify.py output: {result.stdout.strip()}")
        if result.returncode != 0:
            print(f"verify.py failed with code {result.returncode}: {result.stderr}")
            return -1

        # 根据输出判断状态
        output = result.stdout.strip()
        if output == 'True':
            return 0
        elif output == 'False':
            return 1
        elif output.startswith('[') and 'False' in output:
            return 1
        elif output.startswith('[') and output.endswith(']'):
            if output.count('None') > output.count('True'):
                print(f"输出中None比例过高")
                return -1
            else:
                return 0
        else:
            # print('-----------------------')
            # print(output)
            return 2
    except subprocess.TimeoutExpired:
        print(f"verify.py timed out in {workdir}")
        return -1
    except Exception as e:
        print(f"verify.py error: {e}")
        return -1


# ============================================================================
# 任务执行成功后，将文件拷贝到 save_dir 目录下
# ============================================================================


def collect_single_task_outputs(run_dir, save_dir):
    """
    收集任务输出到 save_dir，按工作目录分类拷贝：
    - workdir1: meta.json, task.txt, resource_paths.json 及初始文件
    - workdir2: rubrics.txt, verify.py
    """
    os.makedirs(save_dir, exist_ok=True)

    if not os.path.exists(os.path.join(run_dir, 'state.json')):
        shutil.rmtree(save_dir)
        return False, '无 state.json'

    # print(run_dir)
    state = load_json(os.path.join(run_dir, 'state.json'))
    if not all((c in state or c == 'command2') for c in COMMANDS):
        shutil.rmtree(save_dir)
        return False, '任务未生产完毕'

    workdir1 = os.path.join(run_dir, 'workdir1')
    workdir2 = os.path.join(run_dir, 'workdir2')

    # 从 workdir1 拷贝：meta.json, task.txt, resource_paths.json 及初始文件
    workdir1_files = ['meta.json', 'resource_paths.json']
    for filename in workdir1_files:
        src_file = os.path.join(workdir1, filename)
        if os.path.exists(src_file):
            shutil.copy(src_file, os.path.join(save_dir, filename))

    copy_task_and_resources(workdir1, save_dir)

    # 从 workdir2 拷贝：deliverable_paths.json 及交付件、verify2.py、content_rubrics2.json
    workdir2_files = ['rubrics.txt', 'verify.py', 'deliverable_paths.json']
    for filename in workdir2_files:
        src_file = os.path.join(workdir2, filename)
        if os.path.exists(src_file):
            shutil.copy(src_file, os.path.join(save_dir, filename))

    # 拷贝 deliverable_paths.json 中的交付件
    dp_file = os.path.join(workdir2, 'deliverable_paths.json')
    if os.path.exists(dp_file):
        for deliverable_path in load_json(dp_file):
            deliverable_path = os.path.normpath(deliverable_path)
            _src_path = os.path.join(workdir2, deliverable_path)
            _dst_dir = os.path.dirname(os.path.join(save_dir, deliverable_path))
            if os.path.exists(_src_path):
                os.makedirs(_dst_dir, exist_ok=True)
                shutil.copy(_src_path, _dst_dir)

    return True, '拷贝完成'



def collect_task_outputs(task_pairs, run_verify):
    copy_num = 0
    verify_results = {}  # {task_name: (status, run_dir)}
    for _, run_dir, save_dir in task_pairs:
        flag, output = collect_single_task_outputs(run_dir, save_dir)
        if not flag:
            continue 

        copy_num += 1
        if run_verify:
            # 执行verify.py文件，返回四种状态：
            # -1: 执行失败
            #  0: 执行成功，打印全为True的列表，允许包含None
            #  1: 执行成功，打印列表包含False
            #  2: 执行成功，其他情况
            verify_result = exec_verify(save_dir)
            
            # 收集每条任务的状态
            task_name = os.path.basename(save_dir)
            verify_results[task_name] = (verify_result, run_dir)

    # 统计各状态的数量
    status_counts = {-1: 0, 0: 0, 1: 0, 2: 0}
    for result, _ in verify_results.values():
        if result in status_counts:
            status_counts[result] += 1

    print(f"Verify结果统计: {status_counts}")

    # 删除状态为 -1和2 的任务
    tasks_to_delete = [name for name, (result, run_dir) in verify_results.items() if result in (-1, 2)]
    for task_name in tasks_to_delete:
        task_path = os.path.join(args.save_dir, task_name)
        if os.path.exists(task_path):
            shutil.rmtree(task_path)
            print(f"已删除任务: {task_name} (verify返回{verify_results[task_name][0]})")

    # 打印非全True的情况
    # for task_name, (result, run_dir) in verify_results.items():
    #     if result != 0:
    #         print(f"任务 {task_name} (run_dir: {run_dir}) verify返回: {result}")

    print(f"共删除了 {len(tasks_to_delete)} 个任务")
    print(f'共复制了 {copy_num-len(tasks_to_delete)} 个任务')



# ============================================================================
# 入口
# ============================================================================

"""
python "./skills/oc_pipeline_0508/5. run_cc_requests.py" `
    --model "glm-5.1-thinking" `
    --input_dir "D:/cc/0508/query_by_scenarios/raw" `
    --run_dir "D:/cc/0508/query_by_scenarios/tasks_generation" `
    --save_dir "D:/cc/0508/query_by_scenarios/tasks_output" `
    --skills_dirs "./skills_evaluation/data/skills/skills" `
    --max_workers 25 `
    --limit 10000

python "./skills/oc_pipeline_0508/5. run_cc_requests.py" `
    --model "glm-5.1-thinking" `
    --input_dir "D:/cc/0508/query_by_scenarios/raw" `
    --run_dir "D:/cc/0508/query_by_scenarios/tasks_generation" `
    --save_dir "D:/cc/0508/query_by_scenarios/tasks_output" `
    --skills_dirs "./skills_evaluation/data/skills/skills" `
    --max_workers 25 `
    --limit 10000 --collect_and_verify
"""


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='依次执行 CC 命令')
    parser.add_argument('--model', type=str, default='claude-opus-4-6-thinking')
    parser.add_argument('--max_workers', type=int, default=10)
    parser.add_argument('--input_dir', type=str)
    parser.add_argument('--run_dir', type=str)
    parser.add_argument('--task_timeout', type=int, default=1800)
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--stat', action='store_true')
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--collect_and_verify', action='store_true')
    parser.add_argument('--save_dir', type=str)
    parser.add_argument('--skills_dirs', type=str)

    args = parser.parse_args()

    set_env(model=args.model)

    SKILL_DIRS = args.skills_dirs

    # ============================================================================
    # 统计任务的完成情况
    # ============================================================================
    task_status = {"total": 0, "finished": [], "partial": 0, "not_started": 0, "by_command": {c: [] for c in COMMANDS}}
    for i, name in enumerate(sorted(os.listdir(args.input_dir))):
        src_path = os.path.join(args.input_dir, name)
        if not (os.path.isdir(src_path) and os.path.exists(os.path.join(src_path, 'command1.txt'))):
            continue
        task_status["total"] += 1
        state_file = os.path.join(args.run_dir,  f'{name}', 'state.json')
        if not os.path.exists(state_file):
            task_status["not_started"] += 1
            continue
        try:
            state = load_json(state_file)
        except Exception:
            task_status["not_started"] += 1
            continue
        completed_cmds = [c for c in COMMANDS if c in state]
        for c in completed_cmds:
            task_status["by_command"][c].append(name)
        if len(completed_cmds) == len(COMMANDS):
            task_status["finished"].append(name)
        elif len(completed_cmds) > 0:
            task_status["partial"] += 1
        else:
            task_status["not_started"] += 1

    print(args.run_dir)
    print(f"Task completion summary:")
    print(f"  total:       {task_status['total']}")
    print(f"  finished:    {len(task_status['finished'])}: {task_status['finished'][:20]}")
    print(f"  partial:     {task_status['partial']}")
    print(f"  not_started: {task_status['not_started']}")
    for c in COMMANDS:
        print(f"  {c}: {len(task_status['by_command'][c])}: {task_status['by_command'][c][:20]}")

    if args.stat:
        exit()

    # ============================================================================
    # 收集所有源任务目录
    # ============================================================================
    task_pairs = []
    if os.path.exists(args.input_dir):
        for i, name in enumerate(sorted(os.listdir(args.input_dir), key=lambda s: int(s.split('_')[1]))):
            src_path = os.path.join(args.input_dir, name)
            if os.path.isdir(src_path) and os.path.exists(os.path.join(src_path, 'command1.txt')):
                run_path = os.path.join(args.run_dir,  f'{name}')
                save_path = os.path.join(args.save_dir, f'{name}')
                task_pairs.append((src_path, run_path, save_path))

    print(f"Found {len(task_pairs)} task directories")
    print(f"  input_dir: {args.input_dir}")
    print(f"  run_dir:   {args.run_dir}")


    # ============================================================================
    # 导出文件
    # ============================================================================
    if args.collect or args.collect_and_verify:
        collect_task_outputs(task_pairs, run_verify=(True if args.collect_and_verify else False))
        exit()
    
    # ============================================================================
    # 进行任务生产
    # ============================================================================
    asyncio.run(run_tasks(task_pairs[:args.limit], args.max_workers, args.task_timeout))