"""Deterministic image-coordinate scopes. No C1, screen, network or UI dependencies."""
from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from functools import cached_property
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw, ImageOps, ImageFont

POLICY = {'version': 1, 'max_edge': 1600, 'resize': 'Lanczos, never upscale',
          'signal': 'sRGB encoded Yprime = 0.2126 Rprime + 0.7152 Gprime + 0.0722 Bprime',
          'linear_luminance': 'inverse sRGB transfer then 0.2126 R + 0.7152 G + 0.0722 B',
          'scale': '0..100 percent signal; NOT video IRE', 'histogram_bins': 256,
          'horizontal_summary_bins': 8, 'waveform_signal_bins': 256,
          'near_black_lte': .01, 'near_white_gte': .99,
          'scope_renderer': 'FFmpeg waveform; histogram lines from shared counts; 8-bit full range; luma quantized floor(Yprime*256)',
          'percentile_method': 'linear', 'alpha': 'exclude nonopaque pixels',
          'roi': 'normalized image edges; floor left/top, ceil right/bottom; half-open pixel bounds'}
WEIGHTS = np.array([.2126, .7152, .0722], dtype=np.float64)
_U8 = np.arange(256, dtype=np.float64)/255
_LINEAR = np.where(_U8 <= .04045, _U8/12.92, ((_U8+.055)/1.055)**2.4)
_SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
COLOR_SPACES = {
    'srgb': {'name':'sRGB','weights':[.2126,.7152,.0722],'transfer':'sRGB'},
    'adobe_rgb': {'name':'Adobe RGB (1998)','weights':[.2973449752505362,.6273635662554661,.0752914584939979],'transfer':'gamma 563/256'},
    'display_p3': {'name':'Display P3','weights':[.2289745640697488,.6917385218365064,.079286914093745],'transfer':'sRGB'},
}


def profile_bytes(space):
    if space not in COLOR_SPACES:raise ValueError('Unsupported analysis color space')
    return _SRGB_ICC if space=='srgb' else (Path(__file__).parent/'profiles'/(space+'.icc')).read_bytes()


def policy_for(space):
    spec=COLOR_SPACES[space]
    return {**POLICY,'analysis_color_space':space,'luma_weights':spec['weights'],
            'signal':f"{spec['name']} encoded RGB weighted Yprime approximation",
            'linear_luminance':f"inverse {spec['transfer']} then RGB-to-XYZ Y weights"}


@dataclass
class Pixels:
    rgb: np.ndarray
    valid: np.ndarray
    metadata: dict
    alpha: np.ndarray
    mask_weights: np.ndarray | None = None

    @property
    def space(self):return self.metadata['color'].get('analysis_space_id','srgb')

    @property
    def weights(self):return np.array(COLOR_SPACES[self.space]['weights'])

    @cached_property
    def signal_bins(self):
        encoded=(self.rgb.astype(np.float64)/255) @ self.weights
        return np.clip(np.floor(encoded*256),0,255).astype(np.uint8)

    @cached_property
    def png(self):
        canonical=Image.fromarray(self.rgb)
        canonical.putalpha(Image.fromarray(self.alpha))
        output=io.BytesIO()
        canonical.save(output,format='PNG',compress_level=1,icc_profile=profile_bytes(self.space))
        return output.getvalue()


def decode(data: bytes, color_space='srgb', raw=None) -> Pixels:
    """One canonical sRGB buffer for numerical analysis, scopes and displayed preview."""
    destination=profile_bytes(color_space)
    if len(data) > 64*1024*1024:
        raise ValueError('Image exceeds 64 MiB')
    if raw is not None:
        w,h=raw.get('width'),raw.get('height')
        if (type(w) is not int or type(h) is not int or not 1<=w<=1600 or not 1<=h<=1600
                or raw.get('format')!='BGRA8' or raw.get('bit_depth')!=8 or raw.get('row_order')!='top-down'
                or raw.get('stride')!=w*4 or len(data)!=w*h*4):
            raise ValueError('Invalid native BGRA pixel buffer')
        opened=Image.frombytes('RGBA',(w,h),data,'raw','BGRA')
    else:opened=Image.open(io.BytesIO(data))
    with opened as original:
        if original.width*original.height > 50_000_000:
            raise ValueError('Image exceeds 50 megapixels')
        fmt, size, mode = ('BGRA8' if raw is not None else original.format), list(original.size), original.mode
        if mode not in {'RGB', 'RGBA', 'L', 'LA', 'P'}:
            raise ValueError('First version supports 8-bit RGB/gray images, not RAW, HDR or CMYK')
        if fmt == 'PNG' and data[24] != 8:
            raise ValueError('First version requires an 8-bit PNG; do not silently truncate higher precision')
        if fmt == 'TIFF':
            bits=original.tag_v2.get(258,(8,))
            if any(bit!=8 for bit in (bits if isinstance(bits,tuple) else (bits,))):
                raise ValueError('First version requires 8-bit TIFF samples; high-bit input is not silently truncated')
        original.load()
        im = ImageOps.exif_transpose(original)
        icc = im.info.get('icc_profile')
        alpha = im.convert('RGBA').getchannel('A')
        rgb = im.convert('RGB')
        color = {'analysis_space': COLOR_SPACES[color_space]['name'], 'analysis_space_id':color_space,
                 'source_icc_present': bool(icc),
                 'source_icc_sha256': hashlib.sha256(icc).hexdigest() if icc else None,
                 'confirmation': 'embedded_icc_converted' if icc else 'assumed_sRGB_no_ICC',
                 'conversion': 'ImageCms relative colorimetric directly to selected space, 8-bit; out-of-gamut values clipped' if icc or color_space!='srgb' else 'No transform; assume sRGB encoding and primaries'}
        if icc or color_space!='srgb':
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc or _SRGB_ICC))
            color['source_profile'] = ImageCms.getProfileDescription(profile).strip()
            # Preserve L input for gray profiles; RGB profiles receive RGB pixels.
            source = im.convert('L') if profile.profile.xcolor_space.strip() == 'GRAY' else rgb
            rgb = ImageCms.profileToProfile(source, profile, ImageCms.ImageCmsProfile(io.BytesIO(destination)),
                                            renderingIntent=1, outputMode='RGB')
        rgb.thumbnail((POLICY['max_edge'], POLICY['max_edge']), Image.Resampling.LANCZOS)
        alpha = alpha.resize(rgb.size, Image.Resampling.NEAREST)
        array = np.array(rgb, dtype=np.uint8)
        valid = np.asarray(alpha) == 255
        if not valid.any():
            raise ValueError('No opaque pixels to analyze')
        pixel_hash = hashlib.sha256(str(rgb.size).encode()+array.tobytes()+valid.tobytes()).hexdigest()
        meta = {'source_format': fmt, 'source_size': size, 'source_mode': mode, 'source_bit_depth': 8,
                'analysis_size': list(rgb.size), 'analysis_bit_depth': 8, 'color': color,
                'content_sha256': hashlib.sha256(data).hexdigest(), 'pixel_sha256': pixel_hash,
                'excluded_nonopaque_pixels': int(valid.size-valid.sum()),
                'limitation': 'Preview signal only. Downsampling, 8-bit quantization and any JPEG loss limit precision; near-white pixels do not prove irreversible RAW clipping.'}
        return Pixels(array, valid, meta, np.asarray(alpha))


def roi_bounds(roi, width, height):
    if roi is None:
        roi = [0., 0., 1., 1.]
    if (not isinstance(roi, (list, tuple)) or len(roi) != 4
            or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in roi)):
        raise ValueError('ROI must be [left,top,right,bottom] normalized finite image coordinates')
    l, t, r, b = roi
    if not (0 <= l < r <= 1 and 0 <= t < b <= 1):
        raise ValueError('ROI must have positive area within 0..1')
    return [math.floor(l*width), math.floor(t*height), math.ceil(r*width), math.ceil(b*height)]


def apply_mask(pixels, data, geometry, expected_geometry):
    """Native gray mask is coverage, never a color-managed photograph."""
    if geometry != expected_geometry:
        raise ValueError('Native mask and photo geometry differ; cannot align safely')
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.size != tuple(pixels.metadata['analysis_size']):
            raise ValueError('Native mask and photo sizes differ')
        rgba=np.asarray(im.convert('RGBA'))
        if not (np.array_equal(rgba[:,:,0],rgba[:,:,1]) and np.array_equal(rgba[:,:,1],rgba[:,:,2]) and (rgba[:,:,3]==255).all()):
            raise ValueError('Native mask must be opaque grayscale coverage')
        coverage=rgba[:,:,0].copy()
    weights=coverage.astype(np.float64)/255
    weights[~pixels.valid]=0
    valid=weights>0
    meta={**pixels.metadata,'mask_sha256':hashlib.sha256(data).hexdigest(),
          'sample_mask_sha256':hashlib.sha256(coverage.tobytes()+pixels.valid.tobytes()).hexdigest(),
          'mask_weighting':'native grayscale coverage / 255; no threshold; excludes zero coverage',
          'pixel_sha256':hashlib.sha256(pixels.metadata['pixel_sha256'].encode()+coverage.tobytes()).hexdigest()}
    return Pixels(pixels.rgb,valid,meta,pixels.alpha,weights)


def region_metadata(pixels: Pixels, roi=None):
    h,w=pixels.valid.shape
    l,t,r,b=roi_bounds(roi,w,h)
    count=int(pixels.valid[t:b,l:r].sum())
    return {'roi_normalized':list(roi or [0,0,1,1]),'roi_pixels':[l,t,r,b],'pixel_count':count,
            **({'effective_pixel_count':float(pixels.mask_weights[t:b,l:r].sum()),
                'count_units':'coverage-weighted pixels','percentile_method':'weighted inverse CDF'} if pixels.mask_weights is not None else {})}


def analyze(pixels: Pixels, roi=None):
    h, w = pixels.valid.shape
    l, t, r, b = roi_bounds(roi, w, h)
    rgb = pixels.rgb[t:b, l:r]
    valid = pixels.valid[t:b, l:r]
    if not valid.any():
        raise ValueError('ROI contains no opaque pixels')
    weights=pixels.weights
    encoded = (rgb.astype(np.float64)/255) @ weights
    linear_table=_U8**(563/256) if pixels.space=='adobe_rgb' else _LINEAR
    linear = sum(linear_table[rgb[:,:,i]]*weights[i] for i in range(3))
    signals = {'red': rgb[:,:,0], 'green': rgb[:,:,1], 'blue': rgb[:,:,2], 'luma': encoded}
    quantiles=np.array([1,5,50,95,99])
    coverage=None if pixels.mask_weights is None else pixels.mask_weights[t:b,l:r]
    def summarize(samples, byte_channel=False, sample_weights=None):
        if sample_weights is not None:
            total=sample_weights.sum()
            counts=np.bincount(samples,weights=sample_weights,minlength=256) if byte_channel else None
            values=samples/255 if byte_channel else samples
            if byte_channel:
                sorted_values=_U8;cumulative=np.cumsum(counts)
            else:
                order=np.argsort(values);sorted_values=values[order];cumulative=np.cumsum(sample_weights[order])
            indices=np.minimum(np.searchsorted(cumulative,total*quantiles/100),len(sorted_values)-1)
            percentiles=sorted_values[indices]
            mean=float(values@sample_weights/total)
            near_black=sample_weights[values<=.01].sum()/total;near_white=sample_weights[values>=.99].sum()/total
            return {'mean_percent':mean*100,'percentiles_percent':{f'P{q}':float(v*100) for q,v in zip(quantiles,percentiles)},
                    'near_black_percent':float(near_black*100),'near_white_percent':float(near_white*100)},counts
        if byte_channel:
            counts=np.bincount(samples,minlength=256)
            cumulative=np.cumsum(counts);size=samples.size
            positions=(size-1)*quantiles/100
            low=np.floor(positions).astype(int);high=np.ceil(positions).astype(int)
            lows=np.searchsorted(cumulative,low,side='right')
            highs=np.searchsorted(cumulative,high,side='right')
            percentiles=(lows+(highs-lows)*(positions-low))/255
            mean=float(counts @ _U8/size)
            near_black=counts[:3].sum()/size;near_white=counts[253:].sum()/size
        else:
            counts=None
            percentiles=np.percentile(samples,quantiles,method='linear')
            mean=float(samples.mean());near_black=(samples<=.01).mean();near_white=(samples>=.99).mean()
        return {'mean_percent':mean*100,
                'percentiles_percent':{f'P{q}':float(v*100) for q,v in zip(quantiles,percentiles)},
                'near_black_percent':float(near_black*100),'near_white_percent':float(near_white*100)},counts
    histograms, stats = {}, {}
    for name, signal in {**signals, 'linear_luminance': linear}.items():
        samples = signal[valid]
        stats[name],counts=summarize(samples,name in ('red','green','blue'),None if coverage is None else coverage[valid])
        if name in signals:
            if counts is None:
                bins = np.minimum(255, np.floor(samples*256)).astype(np.uint8)
                counts=np.bincount(bins,weights=None if coverage is None else coverage[valid],minlength=256)
                if name=='luma' and [l,t,r,b]==[0,0,w,h] and valid.all():
                    # The plot consumes the same quantization, not a second full luma pass.
                    pixels.__dict__['signal_bins']=bins.reshape(h,w)
            histograms[name] = counts.tolist()
    horizontal=[]
    count=min(POLICY['horizontal_summary_bins'],r-l)
    for index in range(count):
        a=index*(r-l)//count; z=(index+1)*(r-l)//count
        mask=valid[:,a:z]
        values={}
        for name,signal in signals.items():
            samples=signal[:,a:z][mask]
            if not samples.size:values[name]=None
            else:
                summary,_=summarize(samples,name!='luma',None if coverage is None else coverage[:,a:z][mask]);p=summary.pop('percentiles_percent')
                values[name]={**summary,**{q:p[q] for q in ('P5','P50','P95')}}
        horizontal.append({'image_x_normalized':[(l+a)/w,(l+z)/w],
                           'roi_x_normalized':[a/(r-l),z/(r-l)],'pixel_count':int(mask.sum()),'channels':values})
    return {'roi_normalized': list(roi or [0,0,1,1]), 'roi_pixels': [l,t,r,b],
            **region_metadata(pixels,roi), 'statistics': stats, 'histograms': histograms,
            'horizontal_distribution':horizontal}


def compare(current: dict, reference: dict):
    """Refuse comparisons whose subject or numerical basis differs."""
    for key in ('photo_key', 'source', 'policy', 'analysis_size', 'color', 'crop_signature'):
        if current[key] != reference[key]:
            raise ValueError('Reference is not comparable: '+key+' changed')
    if current.get('sample_mask_sha256')!=reference.get('sample_mask_sha256') or current.get('analysis_scope')!=reference.get('analysis_scope'):
        raise ValueError('Reference mask or analysis scope changed')
    a, b = current['region'], reference['region']
    if a['roi_pixels'] != b['roi_pixels'] or a['pixel_count'] != b['pixel_count']:
        raise ValueError('Reference ROI or sample population changed')
    differences = {}
    for channel, stats in a['statistics'].items():
        old = b['statistics'][channel]
        differences[channel] = {key: ({q: value-old[key][q] for q,value in item.items()} if isinstance(item, dict) else item-old[key]) for key,item in stats.items()}
    return {'reference_id': reference['analysis_id'], 'current_id': current['analysis_id'],
            'units': 'percentage points; current minus reference', 'statistics': differences}


def histogram_height(counts, peak):
    """One decade only, regardless of pixel population; zero remains on the baseline."""
    return np.log10(1+9*np.asarray(counts)/max(1,peak))


def scope_image(pixels: Pixels, roi=None, mode='waveform', vector=None, histograms=None) -> Image.Image:
    """FFmpeg waveform/parade; histogram lines plot the shared numerical definition."""
    if mode=='vectorscope':
        from .vectorscope import calculate,render
        return render(vector if vector is not None else calculate(pixels,roi))
    h,w = pixels.valid.shape
    l,t,r,b = roi_bounds(roi,w,h)
    if pixels.mask_weights is None and not pixels.valid[t:b,l:r].all():
        raise ValueError('Scope rendering requires opaque input; numeric stats exclude transparent pixels')
    rgb = pixels.rgb[t:b,l:r]
    luma = pixels.signal_bins[t:b,l:r] if mode!='parade' else None
    def render(array, components):
        # Same FFmpeg filter in-process: no executable startup or intermediate PNG encode.
        try:import av
        except ImportError as error:
            raise ValueError('Install the scopes extra (or av>=17.1,<18) for waveform/parade images') from error
        frame=av.VideoFrame.from_ndarray(array,format='rgb24')
        frame.time_base=Fraction(1,1);frame.pts=0
        graph=av.filter.Graph();graph.threads=1
        source=graph.add_buffer(template=frame)
        fmt=graph.add('format','pix_fmts=gbrp')
        wave=graph.add('waveform',f'mode=column:display=parade:filter=lowpass:graticule=none:mirror=1:components={components}')
        sink=graph.add('buffersink')
        source.link_to(fmt);fmt.link_to(wave);wave.link_to(sink)
        graph.configure();graph.push(frame)
        return Image.fromarray(graph.pull().to_ndarray(format='rgb24'))
    width, height, left, top = 2048, 720, 112, 64
    plot_w, plot_h = 1888, 560
    canvas = Image.new('RGB', (width,height), '#10141b')
    draw = ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=22)
    colors = {'red': (255,85,90), 'green': (93,235,151), 'blue': (91,151,255), 'luma': (225,231,240)}
    if mode in ('waveform','parade'):
        if pixels.mask_weights is not None:
            coverage=pixels.mask_weights[t:b,l:r];columns=np.broadcast_to(np.arange(r-l),coverage.shape)
            panels=[]
            for i,signal in enumerate([luma] if mode=='waveform' else [rgb[:,:,i] for i in range(3)]):
                density=np.bincount(((255-signal.astype(np.int32))*(r-l)+columns).ravel(),
                    weights=coverage.ravel(),minlength=256*(r-l)).reshape(256,r-l)
                intensity=255*np.log1p(density)/max(1,float(np.log1p(density).max()))
                color=np.ones(3) if mode=='waveform' else np.eye(3)[i]
                panels.append((intensity[:,:,None]*color).astype(np.uint8))
            panel=Image.fromarray(np.concatenate(panels,axis=1))
        else:panel = (render(np.repeat(luma[:,:,None],3,axis=2),1) if mode=='waveform' else render(rgb,7))
        if mode=='waveform':
            # FFmpeg tints RGB traces. Preserve trace density and make Yprime neutral white.
            white=np.array(panel).max(axis=2)
            panel=Image.fromarray(np.repeat(white[:,:,None],3,axis=2))
        canvas.paste(panel.resize((plot_w,plot_h),Image.Resampling.NEAREST),(left,top))
        if mode=='waveform':
            draw.text((left,16),'Yprime waveform',fill='#dbe6f2',font=font)
        else:
            for i,name in enumerate(('RED','GREEN','BLUE')):
                draw.text((left+(i+.5)*plot_w/3,16),name,anchor='mt',fill=colors[name.lower()],font=font)
        for v in (0,25,50,75,100):
            y = top+round((100-v)/100*(plot_h-1))
            draw.line((left,y,left+plot_w,y), fill='#29313e')
            draw.text((12,y-12),f'{v}%',fill='#9faec0',font=font)
        for x,label,anchor in ((left,'0%','lt'),(left+plot_w/2,'50%','mt'),(left+plot_w,'100%','rt')):
            draw.text((x,top+plot_h+24),label,anchor=anchor,fill='#9faec0',font=font)
    elif mode == 'histogram':
        # Render the exact same count arrays returned to AI as lines, without filled areas.
        if histograms is None:
            weights=None if pixels.mask_weights is None else pixels.mask_weights[t:b,l:r].ravel()
            histograms={name:np.bincount(rgb[:,:,i].ravel(),weights=weights,minlength=256) for i,name in enumerate(['red','green','blue'])}
            histograms['luma']=np.bincount(luma.ravel(),weights=weights,minlength=256)
        peak=max(float(max(v)) for v in histograms.values()) or 1
        # Normalize first: log arguments span only 1..10, never 1..millions.
        # Display compression only; the AI still receives unmodified counts.
        for tick in (0,.25,.5,.75,1):
            y=top+round((1-tick)*(plot_h-1));draw.line((left,y,left+plot_w,y),fill='#29313e',width=1)
            draw.text((5,y-12),f'{round(peak*(10**tick-1)/9)}',font=font,fill='#9faec0')
        for name,counts in histograms.items():
            points=[(left+i*(plot_w-1)/255,top+plot_h-1-(plot_h-1)*value) for i,value in enumerate(histogram_height(counts,peak))]
            draw.line(points,fill=colors[name],width=3,joint='curve')
        draw.text((left,16),'RGB + white Yprime histogram | one-decade compression, shared count axis',fill='#dbe6f2',font=font)
        for x,label,anchor in ((left,'0%','lt'),(left+plot_w/2,'Encoded signal','mt'),(left+plot_w,'100%','rt')):
            draw.text((x,top+plot_h+24),label,anchor=anchor,fill='#9faec0',font=font)
    else:
        raise ValueError('Unknown scope mode')
    draw.text((left,688),COLOR_SPACES[pixels.space]['name']+' signal approximation | 8-bit preview | not IRE or RAW clipping',fill='#6f8198',font=font)
    return canvas
