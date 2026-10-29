"""OpenRV Web vectorscope coordinate/graticule adapter, with shared numerical counts.

Adapted from lifeart/openrv-web Vectorscope.ts (MIT, Alex Kanunnikov, 2024),
commit 0ad41bb59713cb6abe710dbab8f4738834e82312. See third_party/openrv-web-MIT.txt.
The RGB-to-CbCr projection, target placement and skin reference geometry are
ported to NumPy/Pillow; ROI, count grids, AI summaries and C1 input are ours.
"""
from functools import lru_cache
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageCms

GRID=513  # Odd size gives neutral gray an exact zero coordinate.
EXTENT=.6
U=np.array([-.169,-.331,.5])
V=np.array([.5,-.419,-.081])


@lru_cache(maxsize=3)
def skin_reference(space):
    # OpenRV Web reference: 123 degrees from +Cb toward +Cr, not skin detection.
    angle=math.radians(123);u,v=.1*math.cos(angle),.1*math.sin(angle)
    rgb=np.linalg.solve(np.array([[.299,.587,.114],U,V]),np.array([.5,u,v]))
    if space!='srgb':
        from .image_analysis import profile_bytes
        import io
        swatch=Image.new('RGB',(1,1),tuple(np.rint(rgb*255).astype(int)))
        converted=ImageCms.profileToProfile(swatch,
            ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes('srgb'))),
            ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes(space))),outputMode='RGB')
        rgb=np.asarray(converted,dtype=np.float64)[0,0]/255
        angle=math.atan2(float(rgb@V),float(rgb@U))
    return {'angle_degrees':math.degrees(angle)%360,'basis':'OpenRV Web 123-degree sRGB reference; reference swatch ICC-converted for other working spaces',
            'sector_half_width_degrees':10,'skin_detection':False,
            'limitation':'A hue reference, not a universal skin target. The sector also contains non-skin colors; use an image ROI.'}


def calculate(pixels,roi=None):
    from .image_analysis import roi_bounds
    h,w=pixels.valid.shape;l,t,r,b=roi_bounds(roi,w,h)
    rgb=pixels.rgb[t:b,l:r];valid=pixels.valid[t:b,l:r]
    if not valid.any():raise ValueError('ROI contains no opaque pixels')
    # Integer coefficients are exact for the upstream three-decimal transform.
    # Avoid a full RGB float copy and two large matrix multiplications each frame.
    red,green,blue=(rgb[:,:,i].astype(np.int32) for i in range(3))
    cb=-169*red-331*green+500*blue;cr=500*red-419*green-81*blue
    ix=np.clip(np.rint(cb*(256/(255000*EXTENT)))+256,0,512).astype(np.int32)
    iy=np.clip(np.rint(cr*(256/(255000*EXTENT)))+256,0,512).astype(np.int32)
    indices=iy*GRID+ix
    samples=indices.ravel() if valid.all() else indices[valid]
    mask_weights=None if pixels.mask_weights is None else pixels.mask_weights[t:b,l:r][valid]
    density=np.bincount(samples,weights=mask_weights,minlength=GRID*GRID).reshape(GRID,GRID)
    yy,xx=np.nonzero(density);counts=density[yy,xx]
    x=(xx-256)*EXTENT/256;y=(yy-256)*EXTENT/256
    radius=np.hypot(x,y);angles=np.degrees(np.arctan2(y,x))%360
    total=float(counts.sum()) if mask_weights is not None else int(counts.sum());chromatic=radius>.02
    colored=float(counts[chromatic].sum()) if mask_weights is not None else int(counts[chromatic].sum())
    reference=skin_reference(pixels.space)
    offsets=(angles-reference['angle_degrees']+180)%360-180
    hue_bins=np.bincount((angles[chromatic]//10).astype(int),weights=counts[chromatic],minlength=36)
    if mask_weights is None:hue_bins=hue_bins.astype(np.int64)
    dx=float(counts[chromatic]@x[chromatic]);dy=float(counts[chromatic]@y[chromatic])
    direction=math.degrees(math.atan2(dy,dx))%360 if math.hypot(dx,dy)>total*1e-6 else None
    summary={'projection':'OpenRV Web BT.601-style projection: Cb=-.169Rprime-.331Gprime+.5Bprime; Cr=.5Rprime-.419Gprime-.081Bprime',
        'working_space':pixels.space,'axis_bounds':[-EXTENT,EXTENT],'grid_size':[GRID,GRID],
        'quantization_step':EXTENT/256,'angle_convention':'degrees counterclockwise from +Cb; +Cr points up',
        'population':total,'sample_pixel_count':int(valid.sum()),
        'population_units':'coverage-weighted pixels' if mask_weights is not None else 'pixels',
        'roi_pixels':[l,t,r,b],'neutral_radius_lte':.02,
        'near_neutral_percent':float(counts[~chromatic].sum()/total*100),
        'mean_cb':float(counts@x/total),'mean_cr':float(counts@y/total),'mean_chroma_radius':float(counts@radius/total),
        'chromatic_pixel_count':colored,'hue_histogram_bin_degrees':10,'hue_histogram_counts':hue_bins.tolist(),
        'chroma_weighted_direction_degrees':direction,
        'direction_offset_from_reference_degrees':None if direction is None else (direction-reference['angle_degrees']+180)%360-180,
        'reference_sector_percent_of_chromatic':float(counts[chromatic & (abs(offsets)<=10)].sum()/colored*100) if colored else None,
        'skin_reference':reference,
        'interpretation':'Encoded working-RGB projection, not a broadcast legal-range or perceptually uniform gamut measurement. Not face or skin detection.'}
    return density,summary


@lru_cache(maxsize=3)
def color_ring(space):
    """Decorative chroma direction key, using the same projection (not an HSV wheel)."""
    axis=np.arange(720)-360;x,y=np.meshgrid(axis,axis);radius=np.hypot(x,y)
    coverage=np.clip(9-np.abs(radius-312),0,1);mask=coverage>0
    signals=np.column_stack([np.full(mask.sum(),.5),.45*x[mask]/radius[mask],-.45*y[mask]/radius[mask]])
    rgb=signals@np.linalg.inv(np.array([[.299,.587,.114],U,V])).T
    colors=Image.fromarray(np.rint(np.clip(rgb,0,1)*255).astype(np.uint8)[None,:,:])
    if space!='srgb':
        from .image_analysis import profile_bytes
        import io
        colors=ImageCms.profileToProfile(colors,ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes(space))),
            ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes('srgb'))),outputMode='RGB')
    layer=np.zeros((720,720,4),dtype=np.uint8)
    layer[mask,:3]=np.asarray(colors)[0];layer[mask,3]=np.rint(coverage[mask]*255).astype(np.uint8)
    return Image.fromarray(layer)


def render(vector):
    density,summary=vector
    # Square output and square display preserve hue angles regardless of window dimensions.
    size=720;left=56;top=56;side=608;center=360;radius=side/2
    canvas=Image.new('RGB',(size,size),'#10141b');draw=ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=21)
    for scale in (.25,.5,.75):
        rad=radius*scale;draw.ellipse((center-rad,center-rad,center+rad,center+rad),outline='#242e3b',width=1)
    draw.line((left,center,left+side,center),fill='#242e3b')
    draw.line((center,top,center,top+side),fill='#242e3b')
    level=np.log1p(density.astype(np.float32));peak=float(level.max()) or 1
    # White dots; logarithmic density affects visibility, not hue/chroma position or counts.
    intensity=np.where(density>0,60+195*level/peak,0).astype(np.uint8)
    trace=Image.fromarray(intensity[::-1]).resize((side,side),Image.Resampling.NEAREST)
    background=canvas.crop((left,top,left+side,top+side))
    panel=Image.fromarray(np.maximum(np.asarray(background),np.repeat(np.asarray(trace)[:,:,None],3,axis=2)))
    canvas.paste(panel,(left,top));draw=ImageDraw.Draw(canvas)
    ring=color_ring(summary['working_space']);canvas.paste(ring,(0,0),ring)
    for label,rgb,color in [('R',[1,0,0],'#ff6b70'),('Y',[1,1,0],'#e6d16a'),('G',[0,1,0],'#77d994'),
                            ('C',[0,1,1],'#72d9dc'),('B',[0,0,1],'#81aaff'),('M',[1,0,1],'#dc8bd9')]:
        hue=math.atan2(float(np.array(rgb)@V),float(np.array(rgb)@U))
        x=center+337*math.cos(hue);y=center-337*math.sin(hue)
        draw.text((x,y),label,anchor='mm',font=font,fill=color)
    angle=math.radians(summary['skin_reference']['angle_degrees'])
    end=(center+radius*.92*math.cos(angle),center-radius*.92*math.sin(angle))
    for distance in range(16,int(radius*.92)-15,14):
        start=(center+distance*math.cos(angle),center-distance*math.sin(angle))
        stop=(center+(distance+7)*math.cos(angle),center-(distance+7)*math.sin(angle))
        draw.line((*start,*stop),fill='#f1d6a0',width=2)
    back=np.array(end)-np.array([math.cos(angle),-math.sin(angle)])*15
    perpendicular=np.array([math.sin(angle),math.cos(angle)])*6
    draw.polygon([end,tuple(back+perpendicular),tuple(back-perpendicular)],fill='#f1d6a0')
    draw.text((end[0]+10,end[1]+14),'Skin ref',anchor='lt',font=ImageFont.load_default(size=18),fill='#e4cfa6')
    return canvas
