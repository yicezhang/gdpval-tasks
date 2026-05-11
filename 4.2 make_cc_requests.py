"""
CC命令生成器（合成任务请求）

功能概述
--------
读取输入的查询数据（JSON 格式），为每条任务在指定输出目录下创建一个独立子目录，
并在其中生成任务元信息及 4 个阶段的 prompt 文件，供后续 CC 流程顺次执行。

目录结构（每个 task_N/）
--------
├── meta.json      # 任务元信息副本
├── command1.txt   # 将 query_content 写入 task.txt
├── command2.txt   # 生成文件大纲（file_outlines.json）和高保真内容（file_content.json）
├── command3.txt   # 生成参考交付件，写入 deliverable_paths.json
└── command4.txt   # 生成评分细则（rubrics.txt）和自动化校验脚本（verify.py）

主要流程
--------
1. 读取输入 JSON 文件（如 query_by_senarios.json）
2. 遍历每条任务，在 output_dir/task_N/ 下创建目录
3. 保存 meta.json 副本
4. 调用 build_command1~4 生成 4 个阶段的 prompt 文件
5. 通过 save_txt 将各 prompt 写入对应的 command{N}.txt
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from tqdm import tqdm

# ============================================================================
# Utility Functions (内嵌实现)
# ============================================================================

def load_json(path, format='json', encoding='utf-8'):
    """加载 JSON 文件，支持普通 JSON 或 line_json 格式。"""
    if format == 'json':
        with open(path, 'r', encoding=encoding) as f:
            return json.load(f)
    elif format == 'line_json':
        data = []
        with open(path, 'r', encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if line:
                    data.append(json.loads(line))
        return data


def save_json(data, path, format='json', encoding='utf-8'):
    """保存 JSON 文件，支持普通 JSON 或 line_json 格式。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if format == 'json':
        with open(path, 'w', encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    elif format == 'line_json':
        with open(path, 'w', encoding=encoding) as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')


def save_txt(content, path, encoding='utf-8'):
    """保存纯文本文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding=encoding) as f:
        f.write(content)


def print_json(data):
    """打印格式化的 JSON。"""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def json_loads(s):
    """解析 JSON 字符串。"""
    return json.loads(s)


def get_hash_id(text: str) -> str:
    """生成文本的 MD5 哈希 ID（取前12位）。"""
    import hashlib
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:12]


# ============================================================================
# Prompt模板
# ============================================================================


def build_command1(line: Dict) -> str:

    TEMPLATE = """将下面的用户请求写入到当前目录下的 task.txt 文件中，并确保文件编码为 UTF-8。

<query_content>
{query_content}
</query_content>
"""

    # 返回格式化后的模板
    return TEMPLATE.format(
        query_content=line['query_content'],
    )


def build_command2(line: Dict) -> str:
    TEMPLATE = """# 角色设定

你是一个高级、专业的全栈文件资源生成专家。你的核心任务是根据用户的需求，一次性完成“文件大纲规划 -> 高保真内容生成 -> 编写执行代码 -> 本地文件生成”的完整工作流。
请注意：你目前在 Claude Code 环境中运行，**你的最终产出必须是实际生成并在本地保存好的物理文件**，而不是输出任务描述或中间过程的JSON。

# 输入信息

## 1. 任务背景与用户意图

{query_content}

## 2. 目标文件元数据

{material_design}

---

# 执行工作流（请自主顺次执行）

## 步骤一：大纲规划与高保真内容构思 (Outline & Content Planning)
在编写代码前，请根据文件描述和要求，构思出完整的文件结构和具体内容：
- **结构清晰**：明确文件需要哪些章节、段落、表头或分页。
- **内容充实且真实**：为每个部分生成具有实际应用价值的完整内容。
- **绝对保真原则**：**坚决杜绝**使用任何形式的占位符（如“此处为正文”、“XX内容待填”、“...”）。所有数据、文本必须是你根据背景合理编造或提取的完整内容！
- 文件大纲保存在当前目录下的 `file_outlines.json` 中。
- 文件具体内容保存在当前目录下的 `file_content.json` 中。

## 步骤二：工具选择与代码编写 (Tool Selection & Coding)
根据 `文件格式` 选择合适的 Python 库（如果环境中没有，请先执行命令安装，如 `pip install reportlab fpdf` 等）：
- 将“步骤一”中构思好的**所有高保真内容**作为数据源，硬编码或写入到你的 Python 脚本中。
- 编写代码以构建目标文件，严格遵循以下类型规范：
  - **DOCX / PDF (文档类)**：具备清晰的文档结构（标题层级、段落样式），表格宽度适配，处理好排版。
  - **XLSX (表格类)**：精准映射数据类型，表头加粗，自动调整列宽，避免内容被遮挡，数据需体现真实性和多样性。
  - **PPTX (演示文稿类)**：合理分配单页信息密度，每页包含标题和核心要点（Bullet Points），避免文字溢出，字号适中。
  - **TXT / MD (文本类)**：强制指定 `encoding='utf-8'`，包含合理的段落结构和 Markdown 语法。
  - **EML (邮件类)**：符合邮件格式规范，避免收件人、发件人以及邮件内容出现乱码。

## 步骤三：执行代码与质量检查 (Execution & Verification)
1. 运行你编写的 Python 脚本，将文件精准保存到目标文件元数据中提供的路径。
2. 确保目录结构存在（若文件的上层文件夹不存在，请在代码中先创建）。
3. 严禁将纯文本直接修改后缀保存为二进制文件格式。

## 注意事项
- 如果文件中有中文，请妥善设置**字体**以防乱码（如注册系统的中文字体SimSun、微软雅黑、或者其他常见字体）。
- 中文不要使用MS Mincho字体。
- 你只需要生成任务所需的初始文件，**严禁执行任务**得到交付件。
"""
    query = line['query_content'] 
    material_design = line['material_design']

    prompt = TEMPLATE.format(
        query_content=json.dumps(query, ensure_ascii=False, indent=2),
        material_design=json.dumps(material_design, ensure_ascii=False, indent=2)
    )

    return prompt 


def build_command3(line: Dict) -> str:
    TEMPLATE = """在当前目录查找task.txt文件，分析其中的用户需求和任务要求，生成符合预期的参考交付件（即预期输出文件）。请确保生成的参考交付件完全满足task.txt中描述的所有内容和格式要求。按照task.txt中的要求保存生成的参考交付件。
    
交付件生成好后，将交付件的相对地址（路径以./开头）写入当前目录的 `deliverable_paths.json`，格式如下。
[
  "path1",
  "path2"
]
"""
    return TEMPLATE


def build_command4(line: Dict) -> str:
    TEMPLATE = """# Role & Context
你是一位资深的 AI 模型输出评估专家（AI Evaluation Architect）和自动化测试工程师。你的专长是根据用户的原始需求和实际交付件，审查并诊断【已有的评分细则（Rubrics）】，将其中的缺陷（如主观模糊、难以通过代码校验等）进行修正，最终重构成绝对客观、高可校验性且 100% 可自动化的新版评分细则，并为其编写健壮的自动化校验脚本。

# Execution Steps (执行步骤)
请严格按照以下步骤执行任务：
1. **定位并读取需求与交付件**：读取当前工作目录下的 `task.txt`（核心需求）以及 `deliverable_paths.json`（交付件路径），并在目录中读取对应的参考文件，理解预期结果。
2. **定位并审查已有细则**：阅读现有的评分细则。评估这些规则是否存在主观模糊、脱离代码解析能力、与 `task.txt` 刚性约束冲突的问题。
3. **修正并重构评分细则**：基于【修正与设计要求】，修复已有细则中的问题（删除无法量化的项，将模糊项改写为代码可测的条件），生成全新且严格的 JSON 格式评分细则，保存至当前目录的 `rubrics.txt`。
4. **编写验证脚本**：编写 `verify.py` 脚本并保存至当前目录，要求该脚本能基于你刚刚修改生成的【新版内容评分细则（rubrics.txt）】对交付件进行自动化解析和判定。
5. **按规范输出**：将最终生成的 JSON 内容与 Python 代码，严格按照下方【输出规范】的格式打印输出，以便外部程序截取保存。

# Input Handling Rules (输入处理规则)
1. **需求至上（Ground Truth）**：`task.txt` 是唯一绝对真理。所有评分细则的修正必须以此为准，确保没有遗漏其中的刚性约束（如确切字数、必须包含的特定字段等）。
2. **已有细则的转化**：原始细则代表了业务意图，但它可能是人类主观视角的。你的任务是“翻译与降级”——如果原始要求是“文笔优美”，你需要将其转化为或替换为代码可检查的客观指标（如“不包含语法错误”、“包含特定关键词”），如果绝对无法代码化，或直接舍弃。
3. **参考件辅助验证**：交付件（Deliverables）用于辅助你理解数据结构，协助你编写更准确的 `verify.py` 脚本。它不一定是100%正确的，若与 `task.txt` 冲突，以 `task.txt` 为准。

# Rubrics Revision & Design Guidelines (评分细则修正与设计要求)
所有准则必须满足以下标准：
- **绝对客观与可校验（Verifiability）**：必须是二元（True/False）且可量化的。
  - 🚫 **错误（原细则常见毛病）**：“排版美观”、“内容丰富”、“逻辑清晰”、“配色舒适”。
  - ✅ **正确（修正后的形态）**：“表格除表头外恰好包含17行数据”、“第三段中包含【总结】字样”、“PPT总页数 >= 4”。
- **全面且不超纲（Comprehensiveness & Accuracy）**：精准覆盖 `task.txt` 中的显性约束，剔除原细则中无中生有的主观考核项。

# 现有的评分细则

{rubrics}

# Output Specification (严格输出要求)
为了确保外部解析程序不会因文本混淆而崩溃，你必须严格使用自定义的 XML 标签来隔离 JSON 结果与 Python 脚本。
**严禁输出任何 Markdown 代码块符号（如 ```json 或 ```python）！严禁输出任何寒暄、解释性文字！**

`rubrics.txt` 请严格遵循以下 JSON 结构，请确保json内容可以被解析。
{{
  "rubrics": {{
    "input_and_output_material": "",
    "criteria": [
      "xx"
    ]
  }}
}}

`verify.py` 脚本需要包含以下内容

```
import json
# 其他必要的基础库导入和函数定义

def verify_main(deliverable_paths: str) -> list[bool]:
    \"\"\"
    接收 deliverable_paths.json 的文件路径。
    返回一个 List[bool]，其长度和顺序必须与 rubrics.txt 中 rubrics.criteria 的数量和顺序严格一致。
    \"\"\"
    # 1. 在此解析 deliverable_paths.json 获取文件列表
    # 2. 在此硬编码解析或加载上面生成的 rubrics 规则
    # 3. 针对每个标准执行自动化校验
    # 4. 返回类似 [True, False, True...] 的结果
    # 5. 允许在评测每个标准的时候，在输出True/False之外，按需输出 None，表示评测不成功
    #    - 比如，判断pptx某一行文字的颜色是否符合要求时，如果未成功读取文字的颜色时，返回 None
    pass # 替换为你的校验逻辑

if __name__ == '__main__':
    result = verify_main(deliverable_paths="./deliverable_paths.json")
    print(result)
```
"""
    rubrics = line['verification_method']
    rubrics['input_and_output_material'] = line['material_design']['input_and_output_material']
    return TEMPLATE.format(
        rubrics=json.dumps(rubrics, ensure_ascii=False, indent=4)
    )


"""
python "./skills/oc_pipeline_0508/4.2 make_cc_requests.py" `
    --input ./skills/oc_pipeline_0508/data/query_by_senarios.json `
    --output_dir D:/cc/0508/query_by_scenarios/raw
"""


if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="CC命令生产器（合成任务请求）")
    parser.add_argument("--input", type=str, default='./skills/oc_pipeline_0508/data/query_by_senarios.json')
    parser.add_argument("--output_dir", type=str, default='D:/cc/0508/query_by_scenarios/raw')
    args = parser.parse_args()

    # 读取输入数据
    input_data = load_json(args.input)

    import random 
    random.seed(42)

    print('加载到任务数量', len(input_data))

    for i, line in enumerate(tqdm(input_data)):
        dirname = f'task_{i}'
        os.makedirs(os.path.join(args.output_dir, dirname), exist_ok=True)

        # 保存 meta.json
        save_json(line, os.path.join(args.output_dir, dirname, 'meta.json'))

        command1 = build_command1(line)
        command2 = build_command2(line)
        command3 = build_command3(line)
        command4 = build_command4(line)

        save_txt(command1, os.path.join(args.output_dir, dirname, 'command1.txt'))
        save_txt(command2, os.path.join(args.output_dir, dirname, 'command2.txt'))
        save_txt(command3, os.path.join(args.output_dir, dirname, 'command3.txt'))
        save_txt(command4, os.path.join(args.output_dir, dirname, 'command4.txt'))
        
    print(f'保存至 {args.output_dir}')
