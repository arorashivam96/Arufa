# Copyright (c) Microsoft. All rights reserved.
import pulumi

pulumi.export("hello", pulumi.Config().require("hello"))
