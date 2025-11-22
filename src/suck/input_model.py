import pydantic


class Model(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True, extra="forbid")


class FileInput(Model):
    url: str
    checksum: str | None = None
    checksum_type: str | None = None


class Input(Model):
    default_checksum_type: str = "md5"
    files: list[FileInput]
