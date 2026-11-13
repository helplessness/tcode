"""``python -m primer_designer.evaluate`` 外部引物评价命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cli_options import add_config_arguments, config_from_args
from .exceptions import PrimerDesignError
from .external import (
    ExternalPrimerEvaluator,
    ExternalPrimerPair,
    evaluation_row,
    export_external_evaluations,
    read_external_primers,
)
from .io import read_sequence_file


def build_parser() -> argparse.ArgumentParser:
    """声明批量文件和命令行单条引物两种评价方式。"""

    parser = argparse.ArgumentParser(
        description="评价网站或其他工具生成的外部突变引物，并按当前算法排名",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sequence", required=True, metavar="FILE", help="母本 FASTA 或纯 DNA 序列文件"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--external-primers",
        "--input",
        dest="external_primers",
        metavar="FILE",
        help="一行一对外部引物的 CSV/XLSX 文件",
    )
    mode.add_argument(
        "--mutation",
        metavar="MUTATION",
        help="单条评价对应的突变，例如 C231A、C231A:GCC、C267NNK",
    )
    single = parser.add_argument_group("单条引物评价")
    single.add_argument(
        "--primer",
        "--forward",
        dest="forward_sequence",
        metavar="SEQUENCE",
        help="正向引物 5′→3′ 序列；与 --mutation 配合使用",
    )
    single.add_argument(
        "--reverse",
        dest="reverse_sequence",
        metavar="SEQUENCE",
        help="可选反向引物 5′→3′ 序列；省略时自动取正向引物的反向互补",
    )
    single.add_argument("--forward-name", help="可选正向引物名称")
    single.add_argument("--reverse-name", help="可选反向引物名称")
    single.add_argument("--source-label", help="可选来源说明，例如 QuikChange 网站")
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="评价结果 .csv/.xlsx；批量模式必填，单条模式可选",
    )
    add_config_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """评价批量文件或单条命令行引物，并返回稳定退出码。"""

    args = build_parser().parse_args(argv)
    try:
        if args.external_primers:
            if not args.output:
                raise PrimerDesignError("批量模式必须通过 --output 指定结果文件")
            if args.forward_sequence or args.reverse_sequence:
                raise PrimerDesignError("批量模式不能同时传 --primer/--reverse")
            external_pairs = read_external_primers(args.external_primers)
        else:
            if not args.forward_sequence:
                raise PrimerDesignError("单条模式必须同时传 --mutation 和 --primer")
            external_pairs = [
                ExternalPrimerPair(
                    mutation=args.mutation,
                    forward_sequence=args.forward_sequence,
                    reverse_sequence=args.reverse_sequence,
                    forward_name=args.forward_name,
                    reverse_name=args.reverse_name,
                    source=args.source_label,
                )
            ]

        config = config_from_args(args)
        evaluations = ExternalPrimerEvaluator(config).evaluate(
            read_sequence_file(args.sequence), external_pairs
        )
        if args.output:
            output = export_external_evaluations(evaluations, Path(args.output))
    except (PrimerDesignError, OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 1

    ranked = sum(item.evaluation_status == "RANKED" for item in evaluations)
    passed = sum(item.constraint_status == "PASS" for item in evaluations)
    if args.external_primers:
        print(f"已评价：{ranked}/{len(evaluations)} 对引物可计算排名，{passed} 对满足约束")
        print(f"结果文件：{output.resolve()}")
    else:
        print(json.dumps(evaluation_row(evaluations[0]), ensure_ascii=False, indent=2))
        if args.output:
            print(f"结果文件：{output.resolve()}")
    return 0 if ranked == len(evaluations) else 2


if __name__ == "__main__":
    raise SystemExit(main())
