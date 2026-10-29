"""Six existing tools plus image analysis using the official MCP SDK's stdio transport.

This process never owns Capture One state or a UIA worker; the per-user owner
outlives MCP client reconnects. The core performs capability/target validation.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult, TextContent
from pydantic import StrictInt

from .client import call
from .formatting import compact_response
from .owner import state_directory
from .guidance import MCP_INSTRUCTIONS

mcp = FastMCP("Capture One AI", instructions=MCP_INSTRUCTIONS)


def _call(command: str, **fields) -> dict[str, Any]:
    result = call({"command": command, **{key: value for key, value in fields.items() if value is not None}})
    return compact_response(result, command=command, action=fields.get("action"))


@mcp.tool()
def context() -> dict[str, Any]:
    """Read application, target, capabilities, shared editing workflow and unresolved operations."""
    return _call("context")


@mcp.tool()
def read(fields: list[str] | None = None, module: str | None = None,
         modules: list[str] | None = None, target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read native parameters with provenance, module guidance/units and an observation token.

    Read fields=[] for target/preview without loading adjustment parameters. Read module=...
    before edits. Parameter targets are absolute, not deltas; skip modules without a visible purpose.
    """
    return _call("read", fields=fields, module=module, modules=modules, target=target)


@mcp.tool()
def adjust(target: dict[str, Any], module: str,
           patch: dict[str, Any], observation_token: str,
           request_id: str | None = None) -> dict[str, Any]:
    """Apply one qualified numeric or structured module using its exact observed module token.

    Structured modules: curves {channel,points:[[x,y],...]}; color_mixer
    {mode:'basic',color,hue?,saturation?,lightness?} or
    {mode:'advanced'|'skin',action:'add'|'update'|'remove',rgb? ,index?,hue?,saturation?,lightness?,smoothness?,uniformity_hue?,uniformity_saturation?,uniformity_lightness?,enabled?,inverted?};
    levels {channel,input_black?,input_white?,midtone?,output_black?,output_white?}; color_balance
    {wheel,x?,y?,lightness?}; layers {action,layer_ref?,creation_operation_id?,opacity?};
    masks {action:'create_subject'|'create_background'}.
    Layers also support rename {layer_ref,name}, set_enabled {layer_ref,enabled},
    duplicate {layer_ref,name?}, move {layer_ref,index}; index is zero-based excluding Background.
    Masks on the selected adjustment layer: invert, clear, fill {density:0..1},
    copy {source_layer} replaces the entire mask; combine {source_layer,mode:add|subtract|intersect};
    feather {radius:0..300}; linear_gradient {start:[x,y],stop:[x,y],midpoint:0.01..0.99};
    radial_gradient {center:[x,y],radius_x,radius_y,falloff:0..1,angle:-360..360,inverted:bool};
    luma_range {shadow,shadow_falloff,highlight,highlight_falloff,radius,sensitivity,inverted}.
    Gradient coordinates/radii use normalized RAW dimensions, not rotated viewer coordinates.
    Luma endpoints 0..255 ordered shadow_falloff<=shadow<=highlight<=highlight_falloff,
    radius 0..300, sensitivity 0..100. select_submask {index} uses the native indexes returned
    by masks read; -1 selects the whole mask. Brush/gradient operations follow C1's selected
    submask semantics. Luma range replaces existing luma components, preserving the other masks.
    Each color module also accepts {action:'reset'} (C1 defaults) or
    {action:'paste',source_observation_token} (a saved complete read of the same module).
    Read the destination module before applying; source snapshot may belong to another layer/photo.
    Native qualification remains required. No presets or style files are created.
    Scalar parameters, levels, all mixers, color wheels and curves share
    the C1 internal model endpoint for Background and the selected adjustment layer.
    Select a layer then re-read the module; target.layer binds every write to that layer.
    Native AI masks use AutoMaskingManager without requiring the tool panel to be visible.
    Levels endpoints use 0..255, midtone -1..1. Advanced H/S/L limits are
    +/-30, -100..200, +/-100; Skin +/-5, +/-30, +/-10. Smoothness 1..30;
    Skin uniformity 0..100. Advanced/Skin may also update hue_low/high and
    saturation_low/high: signed offsets from the source color, low<=-2,
    high>=2, hue span<=359 degrees. Skin has one range. Read gives current
    native ranges and indexes; no screenshot or UI color picking is required.
    """
    return _call("adjust", target=target, module=module, patch=patch,
                 observation_token=observation_token, request_id=request_id)


@mcp.tool()
def images(action: Literal["list", "create_candidate", "select", "next", "previous", "prepare_adjustments", "prepare_tool"],
           target: dict[str, Any] | None = None, observation_token: str | None = None,
           request_id: str | None = None,
           variant_id: StrictInt | None = None,
           variant_uuid: str | None = None, scope: Literal['filtered','all'] = 'filtered',
           offset: StrictInt = 0, limit: StrictInt = 200, collection_token: str | None = None,
           tool: Literal["curves", "levels", "color_mixer", "color_balance", "layers", "masks"] | None = None) -> dict[str, Any]:
    """List the active native collection (including offscreen photos), or select by variant_uuid.

    Follow next_offset with the same collection_token. next/previous follow the filtered collection.
    create_candidate clones the selected variant; edits remain restricted to registered copies.
    """
    return _call("images", action=action, target=target, observation_token=observation_token,
                 request_id=request_id, variant_id=variant_id, variant_uuid=variant_uuid, tool=tool,
                 scope=scope,offset=offset,limit=limit,collection_token=collection_token)


@mcp.tool(structured_output=False)
def preview(target: dict[str, Any], observation_token: str,
            action: Literal["prepare", "render", "restore_destination", "capture", "map_points"] = "render",
            kind: Literal["native", "mask", "render", "viewer"] = "native",
            max_size: StrictInt = 1600, preview_id: str | None = None,
            points: list[dict[str, float]] | None = None,
            direction: Literal['preview_to_raw','raw_to_preview'] = 'preview_to_raw',
            request_id: str | None = None) -> CallToolResult:
    """Default native renders current C1 adjustments asynchronously, without export or focus.

    kind=mask renders the selected layer's whole composite mask. max_size is 128..1600.
    map_points maps {x,y} pixel-edge coordinates using C1's nonlinear transform; use the
    same kind/max_size and returned preview_id. Divide RAW x/y by raw_dimensions to make
    normalized gradient coordinates. A changed photo, layer or revision invalidates the id.
    Only the most recent preview job is retained; eight PNG cache files are retained.
    Legacy kind=render exports, kind=viewer/action=capture samples the display.
    """
    result = _call("preview", target=target, observation_token=observation_token,
                   action=action, kind=kind, max_size=max_size, preview_id=preview_id,
                   points=points,direction=direction,request_id=request_id)
    return _preview_content(result)


def _analysis_proxy(data: bytes, artifact: dict, archive: Path) -> tuple[bytes, dict]:
    """Resize only for visual analysis; never modify or replace the C1 render."""
    from PIL import Image as PillowImage, ImageOps

    with PillowImage.open(io.BytesIO(data)) as original:
        icc = original.info.get("icc_profile")
        source_size = original.size
        frame = ImageOps.exif_transpose(original).convert("RGB")
        frame.thumbnail((1600, 1600), PillowImage.Resampling.LANCZOS, reducing_gap=3.0)
        stream = io.BytesIO()
        options = {"format": "JPEG", "quality": 90, "subsampling": 0, "optimize": True}
        if icc:
            options["icc_profile"] = icc
        frame.save(stream, **options)
        proxy_data, size = stream.getvalue(), frame.size
    digest = hashlib.sha256(proxy_data).hexdigest()
    directory = archive / "analysis-proxies"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.jpg"
    try:
        with path.open("xb") as output:
            output.write(proxy_data)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Existing analysis proxy does not match its hash")
    is_render = artifact.get("source") == "capture_one_export"
    provenance = {"actual_render_path" if is_render else "actual_capture_path": artifact["path"]}
    return proxy_data, {"status": "available", "source": "analysis_proxy", "path": str(path.resolve()),
                        "sha256": digest, "derived_from_sha256": artifact["image"]["sha256"],
                        "derived_from_source": artifact.get("source"), **provenance,
                        "format": "JPEG", "bytes": len(proxy_data),
                        "width": size[0], "height": size[1],
                        "original_width": source_size[0], "original_height": source_size[1],
                        "icc_preserved": bool(icc), "original_render_preserved": is_render,
                        "transform": "EXIF orientation; Lanczos resize to maximum 1600 pixels; JPEG quality 90",
                        "limits": ("Analysis proxy only; use the full C1 render for pixel-level inspection." if is_render
                                   else "Display-capture proxy only; current-render completion and ICC are not verified.")}


def _preview_content(result: dict[str, Any]) -> CallToolResult:
    """Deliver a labelled proxy from a hash-checked render or separately scoped display capture."""
    result = dict(result)
    images = []
    artifact = result.get("result", {})
    if (result.get("status") == "completed" and isinstance(artifact, dict)
            and artifact.get("source") in {"capture_one_export", "viewer_capture", "capture_one_native_render", "capture_one_native_mask"}):
        try:
            path = Path(artifact["path"])
            native=artifact['source'] in {'capture_one_native_render','capture_one_native_mask'}
            archive = (state_directory() / ('native-previews' if native else "renders" if artifact["source"] == "capture_one_export"
                                            else "viewer-captures")).resolve()
            if not path.resolve().is_relative_to(archive) or path.is_symlink():
                raise ValueError("Preview is outside its source-specific owner archive")
            if path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("Render exceeds the analysis input size limit")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact["image"]["sha256"]:
                raise ValueError("Render changed after owner verification")
            if native:
                result['image_delivery']={'status':'available','source':artifact['source'],'path':str(path),
                    'sha256':artifact['image']['sha256'],'format':'PNG','transform':'none'}
                images.append(Image(data=data,format='png').to_image_content())
            else:
                proxy, delivery = _analysis_proxy(data, artifact, archive)
                result["image_delivery"] = delivery
                images.append(Image(data=proxy, format="jpeg").to_image_content())
        except (OSError, KeyError, TypeError, ValueError) as error:
            result["image_delivery"] = {"status": "unavailable", "message": str(error)}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)), *images],
                          structuredContent=result)


@mcp.tool()
def operations(action: Literal["get", "list", "reconcile"],
               operation_id: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    """Read full logs or reconcile current evidence without dispatching recovery writes."""
    return _call("operations", action=action, operation_id=operation_id, request_id=request_id)


@mcp.tool(structured_output=False)
def analyze_image(action: Literal['refresh','status','get','reference','clear_reference','open_window'] = 'refresh',
                  source: Literal['native','file'] = 'native', path: str | None = None,
                  target: dict[str, Any] | None = None, roi: list[float] | None = None,
                  analysis_id: str | None = None, reference_id: str | None = None,
                  color_space: Literal['srgb','adobe_rgb','display_p3'] | None = None,
                  view: Literal['waveform','parade','histogram','vectorscope'] | None = None,
                  include_vectorscope: bool = False,
                  analysis_scope: Literal['image','active_layer'] | None = None,
                  include_histograms: bool = False,
                  include_image: Literal['none','preview','waveform','parade','histogram','vectorscope'] = 'none') -> CallToolResult:
    """Analyze the entire C1 rendered crop (never screen/viewport), or an explicit image file.

    Returns structured RGB/Yprime/linear-Y percentiles, endpoint ratios and horizontal zone values.
    ROI uses normalized IMAGE edges [left,top,right,bottom]. Encoded signal is 0..100%, NOT IRE.
    color_space selects actual ICC conversion to sRGB, Adobe RGB (1998), or Display P3.
    Missing source ICC is still assumed sRGB; a wider target cannot recover missing gamut.
    Read freshness.state: stale/waiting/failed must not be treated as current adjustments.
    analysis_id binds all numbers and optional image to one snapshot. reference pins an existing
    ID; compare via reference_id on refresh/get. Same photo/crop/policy/ROI required.
    get retrieves a window-created ID; status checks revision without rendering. Histograms are
    opt-in exact bin counts. First version uses 8-bit previews and explicit ICC assumptions;
    near-white ratios do not diagnose recoverability of RAW highlights. Native reads preserve
    existing instance/document/variant/layer checks. This tool never edits or exports photos.
    open_window explicitly opens/reuses the human viewer; optional view selects its chart.
    It only acknowledges a launch request, not a fresh image. Other actions need no open window.
    include_vectorscope returns chroma coordinates, 36 hue bins and a skin hue reference.
    analysis_scope=active_layer restricts all statistics/scopes to the current native layer mask,
    weighted by grayscale coverage (feathering retained), intersected with ROI. Background means
    whole image. Default image preserves the old contract. Empty masks return freshness=empty,
    never whole-image fallback. Pixels are final composite colors, not isolated layer colors.
    This reference is not skin detection; use an image ROI for relevant pixels. The vectorscope
    image returns the same numerical summary, with no large density matrix in the response.
    """
    result = call({"command":"analyze_image",**{k:v for k,v in locals().copy().items() if v is not None}})
    images=[]
    artifact=result.get('artifact')
    if artifact:
        try:
            root=(state_directory()/'analysis').resolve()
            file=Path(artifact['path'])
            if file.is_symlink() or file.resolve().parent!=root:
                raise ValueError('Analysis artifact is outside its cache')
            data=file.read_bytes()
            if hashlib.sha256(data).hexdigest()!=artifact['sha256']:
                raise ValueError('Analysis artifact hash mismatch')
            images.append(Image(data=data,format='png').to_image_content())
        except (OSError,KeyError,ValueError) as error:
            result['image_error']=str(error)
    return CallToolResult(content=[TextContent(type='text',text=json.dumps(result,ensure_ascii=False)),*images],
                          structuredContent=result)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
