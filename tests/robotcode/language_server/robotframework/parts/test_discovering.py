import dataclasses
from pathlib import Path

import pytest
import yaml
from pytest_regtest import RegTestFixture

from robotcode.language_server.robotframework.protocol import (
    RobotLanguageServerProtocol,
)


@pytest.mark.usefixtures("protocol")
@pytest.mark.asyncio
async def test_workspace_discovery(
    regtest: RegTestFixture,
    protocol: RobotLanguageServerProtocol,
) -> None:
    from robotcode.language_server.robotframework.parts.discovering import TestItem

    def split(item: TestItem) -> TestItem:
        return dataclasses.replace(
            item,
            id=item.id.split(";", 1)[1] if ";" in item.id else item.id,
            uri="/".join(item.uri.split("/")[-2:]) if item.uri else item.uri,
            children=sorted(
                [split(v) for v in item.children],
                key=lambda v: (
                    v.uri,
                    v.range.start if v.range is not None else None,
                    v.range.end if v.range is not None else None,
                ),
            )
            if item.children
            else item.children,
        )

    result = await protocol.robot_discovering.get_tests_from_workspace(Path(Path(__file__).parent, "data").as_uri())
    regtest.write(
        yaml.dump(
            {
                "result": sorted(
                    (split(v) for v in result),
                    key=lambda v: (v.uri, v.range.start, v.range.end) if v.range is not None else (v.uri, None, None),
                )
                if result
                else result,
            }
        )
    )