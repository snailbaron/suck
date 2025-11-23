`suck` files from the Internet.

## Synopsis

`input.yaml`
```yaml
default_checksum_type: md5

files:
  - url: https://...
    checksum: 0123456789abcdef0123456789abcdef
  - url: https://...
    checksum_type: sha256
    checksum: 0123456789abcdef0123456789abcdef
```

```sh
suck -i input.yaml -o output-directory-with-files
```

## Description

Download files according to a simple YAML config. Existing files with matching checksums are not re-downloaded.

> [!NOTE]  
> Download continuation is not supported. Partially downloaded files will be re-downloaded from the beginning.

* `checksum_type` can be anything Python's [`hashlib.new`](https://docs.python.org/3/library/hashlib.html#hashlib.new) accepts.
* Parallel downloads are done with [`aiohttp`](https://github.com/aio-libs/aiohttp).
* Download progress is displayed with [`rich`](https://github.com/Textualize/rich).

`suck -h`:
```
usage: suck [-h] [-i FILE] [-o DIR] [--clean] [--dump-example-input FILE] [-v] [--max-parallel-downloads N]

options:
  -h, --help            show this help message and exit
  -i, --input FILE      path to the input file
  -o, --output DIR      path to the output directory
  --clean               download to a clean directory
  --dump-example-input FILE
                        dump example input file
  -v, --verbose         print more output
  --max-parallel-downloads N
                        max number of parallel downloads
```

## Bonus

If you want to check downloaded files with `md5sum`/`sha256sum`/whatever, [yq](https://github.com/mikefarah/yq) (a warpper around [jq](https://github.com/jqlang/jq)) can generate them nicely:
```sh
<input.yaml yq -r '.files[] | "\(.checksum)  my-output-dir/\(.url | split("/")[-1])"'
```
