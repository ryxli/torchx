# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import ast
import os
import warnings
from os import path
from typing import Callable, Iterable, List, Type

import torchelastic.tsm.driver as tsm
import yaml
from torchx.cli.cmd_base import SubCommand
from torchx.cli.conf_helpers import parse_args_children


class UnsupportFeatureError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Using unsupported feature {name} in config.")


class ConfValidator(ast.NodeVisitor):
    IMPORT_ALLOWLIST: Iterable[str] = (
        "torchx",
        "torchelastic.tsm",
        "os.path",
        "pytorch.elastic.torchelastic.tsm",
    )

    FEATURE_BLOCKLIST: Iterable[Type[object]] = (
        # statements
        ast.FunctionDef,
        ast.ClassDef,
        ast.Return,
        ast.Delete,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.If,
        ast.With,
        ast.AsyncWith,
        ast.Raise,
        ast.Try,
        ast.Global,
        ast.Nonlocal,
        # expressions
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )

    def visit(self, node: ast.AST) -> None:
        if node.__class__ in self.FEATURE_BLOCKLIST:
            raise UnsupportFeatureError(node.__class__.__name__)

        super().visit(node)

    def _validate_import_path(self, names: List[str]) -> None:
        for name in names:
            if not any(name.startswith(prefix) for prefix in self.IMPORT_ALLOWLIST):
                raise ImportError(
                    f"import {name} not in allowed import prefixes {self.IMPORT_ALLOWLIST}"
                )

    def visit_Import(self, node: ast.Import) -> None:
        self._validate_import_path([alias.name for alias in node.names])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if module := node.module:
            self._validate_import_path([module])


def _get_arg_type(type_name: str) -> Callable[[str], object]:
    TYPES = (int, str, float)
    for t in TYPES:
        if t.__name__ == type_name:
            return t
    raise TypeError(f"unknown argument type {type_name}")


def _parse_run_config(arg: str) -> tsm.RunConfig:
    conf = tsm.RunConfig()
    for key, value in parse_args_children(arg).items():
        conf.set(key, value)
    return conf


# TODO kiuk@ move read_conf_file + _builtins to the Runner once the Runner is API stable

_CONFIG_DIR: str = path.join(path.dirname(__file__), "config")
_CONFIG_EXT = ".torchx"


def read_conf_file(conf_file: str) -> str:
    builtin_conf = path.join(_CONFIG_DIR, conf_file)

    # user provided conf file precedes the builtin config
    # just print a warning but use the user provided one
    if path.exists(conf_file):
        if path.exists(builtin_conf):
            warnings.warn(
                f"The provided config file: {conf_file} overlaps"
                f" with a built-in. It is recommended that you either"
                f" rename the config file or use abs path."
                f" Will use: {path.abspath(conf_file)} for this run."
            )
    else:  # conf_file does not exist fallback to builtin
        conf_file = builtin_conf

    if not path.exists(conf_file):
        raise FileNotFoundError(
            f"{conf_file} does not exist and/or is not a builtin."
            " For a list of available builtins run `torchx builtins`"
        )

    with open(conf_file, "r") as f:
        return f.read()


def _builtins() -> List[str]:
    builtins: List[str] = []
    for f in os.listdir(_CONFIG_DIR):
        _, extension = os.path.splitext(f)
        if f.endswith(_CONFIG_EXT):
            builtins.append(f)

    return builtins


class CmdBuiltins(SubCommand):
    def add_arguments(self, subparser: argparse.ArgumentParser) -> None:
        pass  # no arguments

    def run(self, args: argparse.Namespace) -> None:
        builtin_configs = _builtins()
        num_builtins = len(builtin_configs)
        print(f"Found {num_builtins} builtin configs:")
        for i, name in enumerate(builtin_configs):
            print(f" {i+1:2d}. {name}")


class CmdRun(SubCommand):
    def add_arguments(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--scheduler",
            type=str,
            help="Name of the scheduler to use",
        )
        subparser.add_argument(
            "--scheduler_args",
            type=_parse_run_config,
            help="Arguments to pass to the scheduler (Ex:`cluster=foo,user=bar`)."
            " For a list of scheduler run options run: `torchx runopts`"
            "",
        )
        subparser.add_argument(
            "conf_file",
            type=str,
            help="Name of builtin conf or path of the *.torchx.conf file."
            " for a list of available builtins run:`torchx builtins`",
        )
        subparser.add_argument(
            "conf_args",
            nargs=argparse.REMAINDER,
        )

    def run(self, args: argparse.Namespace) -> None:
        body = read_conf_file(args.conf_file)
        frontmatter, script = body.split("\n---\n")

        conf = yaml.safe_load(frontmatter)
        script_parser = argparse.ArgumentParser(
            prog=f"torchx run {args.conf_file}", description=conf.get("description")
        )
        for arg in conf["arguments"]:
            arg_type = _get_arg_type(arg.get("type", "str"))
            default = arg.get("default")
            if default:
                default = arg_type(default)
            script_args = {
                "help": arg.get("help"),
                "type": arg_type,
                "default": default,
            }
            if arg.get("remainder"):
                script_args["nargs"] = argparse.REMAINDER

            script_parser.add_argument(
                arg["name"],
                **script_args,
            )

        node = ast.parse(script)
        validator = ConfValidator()
        validator.visit(node)

        app = None

        def export(_app: tsm.Application) -> None:
            nonlocal app
            app = _app

        scope = {
            "export": export,
            "args": script_parser.parse_args(args.conf_args),
            "scheduler": args.scheduler,
        }

        exec(script, scope)  # noqa: P204

        assert app is not None, "config file did not export an app"
        assert isinstance(
            app, tsm.Application
        ), f"config file did not export a torchx.spec.Application {app}"

        session = tsm.session()
        app_handle = session.run(app, args.scheduler, args.scheduler_args)
        print(f"Launched app: {app_handle}")
        status = session.status(app_handle)
        print(f"App status: {status}")
        if args.scheduler == "local":
            session.wait(app_handle)
        else:
            print(f"Job URL: {status.ui_url}")
