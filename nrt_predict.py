#!/usr/bin/env python3
"""
Run models on DEA Near-Real-Time (NRT) data.

"""
import numpy as np
import itertools
import argparse
import requests
import hashlib
import joblib
import psutil
import uuid
import yaml
import sys
import os
import re

from osgeo import gdal, ogr, osr
from datetime import datetime, timedelta
from urllib.parse import urlparse
from urllib.request import urlopen, URLError
from pydoc import locate
from uuid import uuid4
from io import BytesIO

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

MODELDIR = "models"

BANDS = ["B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B11", "B12"]

RST = "\033[0m"
RED = "\033[38;5;9m"
GREEN = "\033[38;5;10m"
BLUE = "\033[38;5;4m"


class IncorrectURLError(ValueError):
    """
    Raised if incorrect URL format.
    """


class URLMaybeLocalFileError(ValueError):
    """
    Raised if URL might be a local file.
    """


class IncorrectChecksumError(IOError):
    """
    Raised if the model SHA256 checksum doesn't match what is expected.
    """


def get_s3_client():
    """
    Get a s3 client without the need for credentials.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    return boto3.client('s3', config=Config(signature_version=UNSIGNED))


def check_checksum(f, checksum, blocksize=2<<15):
    """
    Check that the SHA256 checksum of a file-type object matches the 'checksum'. Raises
    an 'IncorrectChecksumError' if they don't match.

    Checksums can be generated on the command line with:

      % shasum -a 256 filename
    
    """
    hasher = hashlib.sha256()
    for block in iter(lambda: f.read(blocksize), b""):
        hasher.update(block)

    actual = hasher.hexdigest()

    log(f"Expected SHA256 checksum: {checksum}")
    log(f"Actual SHA256 checksum: {actual}")

    if actual != checksum:
        raise IncorrectChecksumError()

    log("Checksum matches.")
        

def get_model(name, **args):
    """
    Given 'name', load a `Model` from disk and update its settings.
    This handles the standard case where the models are stored in
    the current directory, the case where a direct path to a pickled
    model is given, and the case where the model is stored in a public
    s3 bucket.
    """
    # Handle case where name is a path a pickled model on disk
    
    if name[:7] == "file://":
        path = name[7:]
        path, checksum = path.split(":")
        log(f"Loading model from '{path}'")
        with open(path, 'rb') as df:
            f = BytesIO(df.read())
            check_checksum(f, checksum)
            f.seek(0)
            model = joblib.load(f)
            model.update(**args)

    # Handle model stored in a s3 bucket
    
    elif name[:5] == "s3://":
        log(f"Loading model from '{name}'")
        bucket, key = name.split('/')[2], name.split('/')[3:]
        key = '/'.join(key)
        with BytesIO() as f:
            s3 = get_s3_client()
            s3.download_fileobj(Bucket=bucket, Key=key, Fileobj=f)
            f.seek(0)
            check_checksum(f, checksum)
            f.seek(0)
            model = joblib.load(f)
            model.update(**args)

    # Handle standard case where model is in the MODELDIR directory
    # in the current working directory

    else:
        modelname = f"{MODELDIR}.{name}"
        sys.path.insert(0, os.getcwd())
        impl = locate(modelname)
        sys.path = sys.path[1:]
        if impl is None:
            raise ImportError(modelname)
        model = impl(**args)

    return model



def check_url(url):
    """
    Simple check to see if it is a valid URL.
    """
    try:
        result = urlparse(url)
    except ValueError:
        raise IncorrectURLError

    if result.scheme == "" or result.scheme == "file":
        raise URLMaybeLocalFileError

    if len(result.path) == 0:
        raise IncorrectURLError


def listfmt(lst):
    """
    Format a list as a str with 4 decimal places of accuracy.
    """
    return '(' + ', '.join([f'{x:.4f}' for x in lst]) + ')'


def wktfmt(wkt):
    """
    Round numbers in WKT str to 4 decimal places of accuracy.
    """
    return re.sub(r"([+-]*\d*\.\d\d\d\d)(\d*)", r"\1", wkt)


def sizefmt(num, suffix="B"):
    """
    Format sizes in a human readible style.
    """
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, "Yi", suffix)


def log(msg="", noinfo=False, color=GREEN):
    """
    Log a message.
    """
    pid = os.getpid()
    process = psutil.Process(pid)
    mem = psutil.virtual_memory()
    used = sizefmt(mem.total - mem.available)
    rss = sizefmt(process.memory_info().rss)
    if noinfo:
        mem = ""
    else:
        mem = f"[MEM {rss}]"
    if isinstance(msg, str) and msg.startswith("#"):
        print("\n" + color + str(msg) + RST + " " + mem + "\n", file=sys.stderr)
    else:
        print(str(msg), file=sys.stderr)


def warning(msg):
    """
    Warning!
    """
    print("\n" + RED + str(msg) + RST, file=sys.stderr)


def parse_pkg(url):
    """
    Package name parsing.
    """
    # TODO: Does this need to be improved for edge cases?
    s = [x for x in url.split("/") if len(x) > 0]
    return s[-1]


def parse_obsdate(url):
    """
    Parse observation date from url.
    """
    pkg = [x for x in url.split("/") if len(x) > 0][-1]
    return datetime.strptime(pkg.split('_')[6], "%Y%m%dT%H%M%S")


def get_bounds(url, **args):
    """
    Return bounds of url as WKT string. Given in EPSG:4326.
    """

    fn = f"{url}/bounds.geojson"

    fd = ogr.Open(fn)

    if fd is None:

        log(f"Opening {url} using GDAL failed, trying alternative...")

        req = requests.get(fn.replace('/vsicurl/', ''))
        fn = "/vsimem/bounds.geojson"
        gdal.FileFromMemBuffer(fn, req.content)
        fd = ogr.Open(fn)

    layer = fd.GetLayer()
    ftr = layer.GetFeature(0)
    poly = ftr.GetGeometryRef()
    return poly.ExportToWkt()


def get_observation(url, product="NBAR", onlymask=False, **args):
    """
    Get the NRT observation from the S3 or public (HTTP) bucket and load the
    data into memory in a numpy array of shape (ysize, xsize, nbands). This is
    assuming the DEA package format.
    """

    pkg = parse_pkg(url)

    stripped_url = url.replace('/vsicurl/', '')

    fn = f"{url}/QA/{pkg}_FMASK.TIF"
    fd = gdal.Open(fn)
    mask = fd.ReadAsArray()

    # TODO: use these stats to throw exception if the observation is too cloudy?
    
    pnodata = np.count_nonzero(mask == 0) / np.prod(mask.shape)
    pclear = np.count_nonzero(mask == 1) / np.prod(mask.shape)

    log(f"Package:   {pkg}")
    log(f"Thumbnail: {stripped_url}/{product}/{product}_THUMBNAIL.JPG")
    log(f"Location:  {stripped_url}/map.html")
    log(f"Pixels:    {mask.shape[0]} x {mask.shape[1]}")
    log(f"Clear %:   {pclear:.4f}")


    geo = fd.GetGeoTransform()
    prj = fd.GetProjection()

    ysize = mask.shape[0]
    xsize = mask.shape[1]

    if onlymask:
        return (geo, prj, mask[:,:,np.newaxis])

    log("# Loading data")

    # Load other bands

    data = np.empty((ysize, xsize, len(BANDS)), dtype=np.float32)
    for i, band in enumerate(BANDS):
        fn = f"{url}/{product}/{product}_{band}.TIF"
        log(f"Loading band {band}")
        fd = gdal.Open(fn)
        fd.ReadAsArray(buf_obj=data[:, :, i], buf_ysize=ysize, buf_xsize=xsize)

    # TODO: set in config?

    data /= 10000
    data[mask == 0] = np.nan

    log(f"data shape: {data.shape}")

    return (geo, prj, data)


def polygon_from_geobox(geo, xsize, ysize):
    """
    Generate a polygon from a geobox and the number of pixels.
    """
    poly = ogr.Geometry(ogr.wkbPolygon)

    px = np.empty(5, dtype=np.float64)
    py = np.empty(5, dtype=np.float64)

    px[0] = geo[0] + 0 * geo[1] + 0 * geo[2]
    py[0] = geo[3] + 0 * geo[4] + 0 * geo[5]

    px[1] = geo[0] + xsize * geo[1] + 0 * geo[2]
    py[1] = geo[3] + xsize * geo[4] + 0 * geo[5]

    px[2] = geo[0] + xsize * geo[1] + ysize * geo[2]
    py[2] = geo[3] + 0 * geo[4] + ysize * geo[5]

    px[3] = geo[0] + 0 * geo[1] + ysize * geo[2]
    py[3] = geo[3] + 0 * geo[4] + ysize * geo[5]

    px[4] = geo[0] + 0 * geo[1] + 0 * geo[2]
    py[4] = geo[3] + 0 * geo[4] + 0 * geo[5]

    ring = ogr.Geometry(ogr.wkbLinearRing)
    for i in range(5):
        ring.AddPoint(px[i], py[i])

    poly.AddGeometry(ring)

    return poly


def generate_clip_shape_from(fn, shpfn):
    """
    Take a filename to a raster object (could be in /vsimem) and
    generate the shape of the observation.
    """
    fd = gdal.Open(fn)

    geo = fd.GetGeoTransform()

    xsize = fd.RasterXSize
    ysize = fd.RasterYSize

    driver = ogr.GetDriverByName("GeoJSON")
    ds = driver.CreateDataSource(shpfn)

    srs = osr.SpatialReference()
    srs.ImportFromWkt(fd.GetProjectionRef())
    layer = ds.CreateLayer("obs", srs)

    poly = polygon_from_geobox(geo, xsize, ysize)

    log(f"wkt: {poly.ExportToWkt()}")

    fdef = layer.GetLayerDefn()
    oftr = ogr.Feature(fdef)

    oftr.SetGeometry(poly)

    layer.CreateFeature(oftr)

    oftr = None
    ds = None

    return shpfn


def run(
    url=None,
    urlprefix=None,
    obstmp=None,
    clipshpfn=None,
    inputs=None,
    tmpdir=None,
    models=None,
    hotspots_only=False,
    hotspots_username=None,
    hotspots_password=None,
    hotspots_resolution=None,
    hotspots_maxpoints=None,
    hotspots_translate=None,
    hotspots_probclip=None,
    **args,
):
    """
    Load and prepare the data required for the change detection algorithms
    and then pass this data to the algorithm. Use `args` to parametrise.
    """
    try:
        check_url(url)

        if urlprefix:
            url = f"{urlprefix}/{url}"

    except URLMaybeLocalFileError:
        if os.path.exists(url):
            log("\nObservation data url seems to be a local file, continuing anyway...")

    log("# Retrieving NRT observation details")

    obswkt = get_bounds(url)
    obsdate = parse_obsdate(url)

    log(f"Obs. Date: {obsdate}")
    log(f"Obs. WKT:  {wktfmt(obswkt)}")

    geo, prj, obsdata = get_observation(url)

    ysize, xsize, psize = obsdata.shape

    # TODO: Option to scale?

    log(f"Creating {obstmp}")
    driver = gdal.GetDriverByName("GTiff")
    fd = driver.Create(obstmp, xsize, ysize, psize, gdal.GDT_Float32)
    fd.SetGeoTransform(geo)
    fd.SetProjection(prj)
    for i in range(fd.RasterCount):
        ob = fd.GetRasterBand(i + 1)
        ob.WriteArray(obsdata[:, :, i])
        ob.SetNoDataValue(0)
    del fd

    log(f"# Preparing ancillary data")

    log(f"Determining ancillary files required")

    outputs = []
    inputs = []
    for model in models:
        name = model["name"]

        log(f"Checking {name}")

        output = model['output']

        ips = model['inputs']
        for ip in ips:
            fn = ip['filename']
            if fn not in outputs:
                inputs.append(fn)

        outputs.append(output)

    # Get the unique inputs

    inputs = [*{*inputs}]

    log(f"Inputs: {inputs}")

    log("Determining observation shape")
    clipshpfn = generate_clip_shape_from(obstmp, clipshpfn)

    #log(f"Removing {obstmp}")
    #gdal.Unlink(obstmp)

    # TODO: Option for multiple reference images?

    log(f"# Warping and clipping ancillary data")

    datamap = {}

    for afn in inputs:
        log(f"Clipping and warping input '{afn}'")

        ofn = f"{tmpdir}/{uuid.uuid4()}"
        fd = gdal.Warp(ofn, afn, cutlineDSName=clipshpfn, cropToCutline=True, dstSRS=prj)

        nbands = fd.RasterCount
        nodata = fd.GetRasterBand(1).GetNoDataValue()
        data = np.empty((ysize, xsize, nbands), dtype=np.float32)
        for i in range(nbands):
            band = fd.GetRasterBand(i + 1)
            band.ReadAsArray(
                buf_type=gdal.GDT_Float32,
                buf_xsize=xsize,
                buf_ysize=ysize,
                buf_obj=data[:, :, i],
            )

        data[data == nodata] = np.nan

        nnan = np.count_nonzero(np.isnan(data))
        nval = xsize * ysize * nbands
        pnan = nnan / nval
        if pnan > 0.9:
            warning(f"clipped input '{afn}' has more than 90% no data")

        gdal.Unlink(ofn)

        datamap[afn] = data

    # TODO: make this changeable
    # TODO: list of models, different outputs for each one so they can be chained?
    
    for m in models:

        name = m.pop("name")
        outfn = m.pop("output")
        inputs = m.pop("inputs")

        log(f"# Model: {name}")

        m['obswkt'] = obswkt
        m['obsdate'] = obsdate
        m['geo'] = geo
        m['prj'] = prj
        m['xsize'] = xsize
        m['ysize'] = ysize

        try:

            model = get_model(name, **m)

        except IncorrectChecksumError as e:
            warning(f"Model has an incorrect SHA256 checksum, exiting...")
            sys.exit(1)

        # Prepare all the appropriate ancillary data sets and pass the
        # observation data as the last one in the list.
        
        datas = []
        for ip in inputs:

            fn = ip['filename']
            data = datamap[fn].copy()

            #TODO: scale, etc?

            datas.append(data)

        datas.append(obsdata.copy())

        # The model is responsible for saving its prediction to disk (or memory
        # using /vsimem) as it is best placed to make a decision on the format, etc.
        # A simple model only needs to implement the `predict` method but can also
        # implement `predict_and_save` if more control of writing output is needed.

        try:

            model.predict_and_save(datas, outfn)

        except Exception as e:
            traceback = e.__traceback__
            warning(f"Error in model '{name}': {e}, see line {traceback.tb_lineno}.")
            sys.exit(1)


def astuple(v):
    """
    Cast to tuple but handle the case where 'v' could be a
    str of values '(a,b,c,...)' and then parse the values
    as floats.
    """
    if isinstance(v, str):
        return tuple([float(x) for x in v[1:-1].split(',')])
    else:
        return tuple(v)


def check_config(args):
    """
    Check config and set some defaults if necessary.
    """
    log(f"# Checking configuration")

    errors = False

    # Set GDAL config
    
    for k,v in args['gdalconfig'].items():
        if v == True:
            v = 'YES'
        if v == False:
            v = 'NO'
        gdal.SetConfigOption(k, v)
        log(f'GDAL option {k} = {v}')

    
    # Set some defaults

    defaults = [
        ("quiet", False),
        ("product", "NBAR"),
        ("obstmp", "/vsimem/obs.tif"),
        ("urlprefix", ""),
        ("tmpdir", "/tmp"),
        ("gdalconfig", {}),
    ]

    for arg, value in defaults:
        if not arg in args:
            log(f"'{arg}' not set, setting to default: {arg} = {value}")
            args[arg] = value

    nonnull = ["models"]
    for s in nonnull:
        if not s in args:
            warning(f"error: '{s}' should be set in the configuration.")
            errors = True

    if not isinstance(args["models"], list):
        log("'models' must be a list of models")
        errors = True


    # Set some model defaults
    
    avail = [gdal.GetDriver(i).ShortName for i in range(gdal.GetDriverCount())]

    # Check the models

    models = args["models"]
    for m in models: 

        name = m["name"]

        if name[:7] == "file://":
            try:
                path = name[7:]
                path, checksum = path.split(":")
            except ValueError:
                log(f"Incorrect model name format, it should be file://filename:sha256checksum")
                errors = True
            
        if name[:5] == "s3://":
            try:
                path = name[5:]
                path, checksum = path.split(":")
            except ValueError:
                log(f"Incorrect model name format, it should be s3://bucket/key:sha256checksum")
                errors = True
 
        if "driver" not in m:
            m["driver"] = "GTiff"

        if m["driver"] not in avail:
            log(f"'driver' for model '{name}' is not available")
            log(f"available drivers: {avail}")
            errors = True

        if "inputs" not in m:
            m["inputs"] = []

        # Check existence of input files
    
        for ip in m["inputs"]:
            fn = ip['filename']
            try:
                fd = gdal.Open(fn)
            except RuntimeError as e:
                warning(f"input file error: {e}")
                errors = True

        # Parse tuples as tuples of numbers for models
        
        for k,v in m.items():
            if isinstance(v, dict):
                for kk,vv in v.items():
                    if isinstance(vv, str) and vv.startswith('(') and vv.endswith(')'):
                        v[kk] = astuple(vv)

    args["models"] = models

    if errors:
        sys.exit(1)

    # TODO: Check GPU resources if needed?
    
    return args


def main(url=None):
    """
    Parse settings from command line and settings file into `args`,
    run, and CLI interface.
    """
    parser = argparse.ArgumentParser()

    if url is None:
        parser.add_argument("url")

    parser.add_argument("-config", default="nrt_predict.yaml", metavar=('yamlfile'))

    # ...

    args = vars(parser.parse_args())

    if url:
        args["url"] = url

    # Try to load configuration file.

    try:

        fn = args["config"]
        with open(fn) as fd:
            args = {**args, **yaml.safe_load(fd)}

        log(f"Loading configuration from '{fn}'")

    except yaml.parser.ParserError as e:
        warning(f"Configuration file '{fn}' has incorrect YAML syntax: {e}")
        warning("\nContinuing without configuration file...")

    except FileNotFoundError:
        warning(f"Configuration file '{fn}' not found.")
        warning("\nContinuing without configuration file...")

    # Run this thing

    try:
        args = check_config(args)

        run(**args)

    except IncorrectURLError:
        warning(f"Error: incorrect URL given: '{url}'")

    except RuntimeError as e:
        warning(f"Error: {e}")

    except KeyboardInterrupt:
        warning(f"Processing interrupted, exiting...")


if __name__ == "__main__":
    # url = "https://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/2021-01-29/S2A_OPER_MSI_ARD_TL_EPAE_20210129T023046_A029271_T54KWA_N02.09"
    # url = "https://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/2021-02-05/S2A_OPER_MSI_ARD_TL_VGS1_20210205T055002_A029372_T50HMK_N02.09"
    url = "S2A_OPER_MSI_ARD_TL_VGS1_20210205T055002_A029372_T50HMK_N02.09"
    #url = "https://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/2021-02-26/S2A_OPER_MSI_ARD_TL_EPAE_20210226T014820_A029671_T55HED_N02.09"
    #url = "http://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/2021-02-26/S2A_OPER_MSI_ARD_TL_EPAE_20210226T014820_A029671_T55HDD_N02.09"
    #url = "https://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/2021-03-13/S2B_OPER_MSI_ARD_TL_VGS4_20210313T012448_A020977_T55HED_N02.09"

    main(url)
