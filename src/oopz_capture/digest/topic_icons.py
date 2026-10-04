"""Original finite topic-icon library. No network, SVG, evaluation or model paths.
A model may select only a category string; unknown categories use the same neutral
conversation symbol. All artwork is deterministic, local Pillow vector geometry.
"""
from __future__ import annotations
from typing import Any

ICON_CATEGORY_TO_KIND = {
    "other":"bubble", "conversation":"bubble", "voice":"voice",
    "ai":"chip", "gaming":"game", "food":"bowl", "food_seafood":"crab",
    "shopping":"ticket", "planning":"clock", "travel":"route",
    "work_learning":"book", "music":"music", "media":"play",
}
# Backward-compatible trusted template aliases are also finite, never paths.
_ALIASES = {"bubble":"conversation","chip":"ai","game":"gaming",
            "crab":"food_seafood","ticket":"shopping","clock":"planning"}

def normalize_category(value: Any) -> str:
    if not isinstance(value,str):
        return "other"
    value=value.strip().lower()
    value=_ALIASES.get(value,value)
    return value if value in ICON_CATEGORY_TO_KIND else "other"

def icon_kind(value: Any) -> str:
    return ICON_CATEGORY_TO_KIND[normalize_category(value)]

def draw_topic_icon(draw,x:float,y:float,size:float,category:Any,color:str,
                    background:str="#081222",scale:int=1) -> str:
    category=normalize_category(category)
    _draw_geometry(draw,ICON_CATEGORY_TO_KIND[category],x,y,size,color,background,scale)
    return category

def _draw_geometry(draw,kind,x,y,size,color,bg='#081222',scale=1):
    d=draw;S=scale
    k=size/64
    def P(a,b): return ((x+a*k)*S,(y+b*k)*S)
    def L(pts,w=3): d.line([P(a,b) for a,b in pts],fill=color,width=max(1,round(w*k*S)),joint='curve')
    def E(box,fill=None,w=3):d.ellipse((*P(box[0],box[1]),*P(box[2],box[3])),fill=fill,outline=color,width=max(1,round(w*k*S)))
    def R(box,r=6,fill=None,w=3):d.rounded_rectangle((*P(box[0],box[1]),*P(box[2],box[3])),radius=r*k*S,fill=fill,outline=color,width=max(1,round(w*k*S)))
    if kind=='ticket':
        R((6,16,58,49),6)
        E((1,27,13,39),bg,2);E((51,27,63,39),bg,2)
        for yy in [22,30,38]:L([(42,yy),(42,yy+4)],2)
        L([(17,25),(31,25)],3);L([(17,34),(28,34)],3)
    elif kind=='chip':
        R((16,16,48,48),5);R((25,25,39,39),3,fill=color,w=1)
        for v in [23,32,41]:
            L([(v,7),(v,16)],2);L([(v,48),(v,57)],2)
            L([(7,v),(16,v)],2);L([(48,v),(57,v)],2)
    elif kind=='game':
        L([(16,21),(24,18),(40,18),(48,21),(57,44),(54,49),(48,49),(39,39),(25,39),(16,49),(10,49),(7,44),(16,21)],3)
        L([(20,25),(20,36)],3);L([(14,30),(26,30)],3)
        E((39,26,43,30),color,1);E((46,32,50,36),color,1)
    elif kind=='crab':
        E((19,27,45,47),None,3)
        for side in [-1,1]:
            cx=32+side*14
            L([(cx,32),(cx+side*10,23),(cx+side*12,11)],3)
            L([(cx+side*10,23),(cx+side*19,17),(cx+side*20,8)],3)
            for yy in [35,42]:L([(32+side*12,yy),(32+side*22,yy+3),(32+side*27,yy+10)],2)
        L([(27,26),(25,20)],2);L([(37,26),(39,20)],2)
        E((22,16,27,21),color,1);E((37,16,42,21),color,1)
    elif kind=='voice':
        d.arc((*P(12,8),*P(52,49)),180,360,fill=color,width=max(1,round(3*k*S)))
        R((8,29,19,51),5);R((45,29,56,51),5)
        L([(49,51),(43,57),(34,57)],3)
        E((29,54,36,60),color,1)
    elif kind=='clock':
        E((11,11,53,53),None,3);L([(32,20),(32,33),(42,38)],3)
    elif kind=='spark':
        L([(32,5),(37,24),(57,32),(37,38),(32,57),(26,38),(7,32),(26,24),(32,5)],3)
    elif kind=='bubble':
        R((8,10,56,45),7);L([(38,45),(45,55),(46,44)],3)
        L([(18,22),(45,22)],3);L([(18,32),(37,32)],3)

    elif kind=='bowl':
        L([(10,27),(54,27),(49,43),(40,50),(24,50),(15,43),(10,27)],3)
        L([(20,56),(44,56)],3)
        L([(22,20),(20,14),(23,8)],2);L([(34,20),(32,14),(35,8)],2)
    elif kind=='route':
        E((7,7,21,21),None,3);E((43,43,57,57),None,3)
        L([(15,25),(15,42),(24,49),(35,49)],3)
        L([(36,44),(42,49),(36,54)],3)
    elif kind=='book':
        L([(32,18),(22,11),(7,11),(7,49),(22,49),(32,55),(42,49),(57,49),(57,11),(42,11),(32,18),(32,55)],3)
        L([(15,23),(24,23)],2);L([(15,32),(24,32)],2);L([(40,23),(49,23)],2)
    elif kind=='music':
        L([(23,46),(23,15),(49,9),(49,39)],3);L([(23,23),(49,17)],3)
        E((9,39,24,51),None,3);E((35,32,50,44),None,3)
    elif kind=='play':
        R((6,12,58,51),7);L([(27,22),(41,31),(27,41),(27,22)],3)
