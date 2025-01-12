"""Harvests a K2 Campaign's target pixel file metadata from MAST.

Note that we avoid accessing "fits.open(fn)[1].data", as it would make
astropy load all the data into memory if the file is gzipped. 
"""
import os
import sys
import time
import multiprocessing
from collections import OrderedDict
try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen

from astropy import log
from astropy.io import fits
from astropy.utils.console import ProgressBar


log.setLevel("INFO")

# Configuration constants
# Local directory containing a mirror of MAST TPF files:
DATA_STORE = "/media/gb/kdata/k2/target_pixel_files"
TMPDIR = "/data/tmp/"   # Location to download temporary files from MAST if needed
MAX_ATTEMPTS = 50  # How many times do we try to obtain & open a file?
SLEEP_BETWEEN_ATTEMPTS = 30  # seconds
IGNORE_SHORT_CADENCE = False


class TargetPixelFile(object):
    """Represent a Target Pixel File (TPF) as obtained from the MAST archive.

    Parameters
    ----------
    path : str
        Path or url to the tpf file.

    url : str, optional
        Public URL of the file, to be stored as metadata.
        (Defaults to the value of `path`.)
    """
    def __init__(self, path, url=None):
        self.path = path.strip()
        self.url = url
        if url is None:
            self.url = self.path
        attempt = 1
        while attempt <= MAX_ATTEMPTS:
            try:
                self.fits = fits.open(path, memmap=True)
                attempt = 99
            except Exception as e:
                if attempt == MAX_ATTEMPTS:
                    log.error("{}: max attempts reached".format(path))
                    raise e
                # Else try again after a sleep
                log.warning("{}: attempt {} failed: {}".format(path, attempt, e))
                log.warning("Now sleeping for {} sec".format(SLEEP_BETWEEN_ATTEMPTS))
                time.sleep(SLEEP_BETWEEN_ATTEMPTS)
                attempt += 1

    def header(self, kw, ext=0):
        """Returns the FITS header keyword for a specified extension.

        Returns the empty string if a keyword is Undefined or non-existant.
        """
        try:
            value = self.fits[ext].header[kw]
        except KeyError:  # Keyword does not exist
            return ""
        if isinstance(value, fits.Undefined):  # Keyword undefined
            return ""
        return value

    def get_metadata(self):
        """Returns a dictionary containing only the metadata we care about."""
        meta = OrderedDict()
        meta["filename"] = os.path.basename(self.url)
        meta["url"] = self.url
        filesize_mb = os.path.getsize(self.path) / 1048576.  # MB
        meta["filesize"] = "{:.1f}".format(filesize_mb)
        for kw in ["OBJECT", "KEPLERID", "OBSMODE", "CAMPAIGN", "DATA_REL",
                   "CHANNEL", "MODULE", "OUTPUT",
                   "RA_OBJ", "DEC_OBJ", "KEPMAG"]:
            meta[kw] = self.header(kw)
        meta["cadences"] = self.fits[1].header["NAXIS2"]
        for kw in ["LC_START", "LC_END", "GAIN", "READNOIS", "MEANBLCK",
                   "CDPP3_0", "CDPP6_0", "CDPP12_0"]:
            meta[kw] = self.header(kw, ext=1)
        meta["npix"] = (self.fits[2].data > 0).sum()  # No of pixels downlinked
        for kw in ["NAXIS1", "NAXIS2", "CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2",
                   "CDELT1", "CDELT2", "PC1_1", "PC1_2", "PC2_1", "PC2_2",
                   "CRVAL1P", "CRVAL2P"]:
            meta[kw] = self.header(kw, ext=2)
        return meta

    def get_csv_header(self):
        """Returns the header line for the csv file."""
        meta = self.get_metadata()
        return ",".join([kw.lower() for kw in meta])

    def get_csv_row(self):
        """Returns the data line for the csv file."""
        meta = self.get_metadata()
        # Surely the values will never contain a comma right?
        return ",".join([str(meta[kw]) for kw in meta])


def download_file(url, local_filename, chunksize=16*1024):
    """Download a large file straight to disk."""
    response = urlopen(url)
    with open(local_filename, 'wb') as f:
        while True:
            chunk = response.read(chunksize)
            if not chunk:
                break
            f.write(chunk)


def get_metadata_row(url, header=False, data_store=DATA_STORE):
    """
    Parameters
    ----------
    data_store : str, optional
        Path to a local directory where the contents of the
        `archive.stsci.edu/pub/k2/target_pixel_files` are mirrored.
        If `None` then all data will be downloaded.  (Default: None.) 
    """
    output = ""
    url = url.strip()
    # Ignore short cadence files?
    if IGNORE_SHORT_CADENCE and "spd-targ" in url:
        return None
    # Try opening the file and adding a csv row
    try:
        tmp_download = False
        localpath = url.replace("http://archive.stsci.edu/missions/k2/target_pixel_files", DATA_STORE)
        if os.path.exists(localpath):
            path = localpath
        else:
            log.debug("Downloading {}".format(url))
            path = os.path.join(TMPDIR, os.path.basename(url))
            download_file(url, path)
            tmp_download = True

        log.debug("Reading {}".format(path))
        tpf = TargetPixelFile(path, url=url)
        if header:
            output += tpf.get_csv_header() + "\n"
        output += tpf.get_csv_row() + "\n"
    except Exception as e:
        log.error("{}: {}".format(url, e))
    finally:
        # Ensure the temporary file is deleted
        if tmp_download:
            log.debug("Removing {}".format(path))
            try:
                os.unlink(path)
            except Exception as e:
                log.error("Could not delete {}: {}".format(url, e))
    return output


def write_metadata_table(input_fn, output_fn):
    """
    Parameters
    ----------
    input_fn : str
        Path to a text file listing the URLs of all the target pixel files
        to be analyzed.

    output_fn : str
        Path to the csv file that will be created.  If the file already exists,
        it will be overwritten.
    """
    # Main routine: download target pixel fiels & produce the metadata table
    with open(output_fn, "w") as out:
        with open(input_fn, "r") as urls:
            urls = urls.readlines()
            with ProgressBar(len(urls)) as bar:
                # Write the first file with a header
                out.write(get_metadata_row(urls[0], header=True))
                # Then process the others in parallel without header
                p = multiprocessing.Pool()
                for idx, result in enumerate(
                    p.imap_unordered(get_metadata_row, urls[1:], chunksize=3)):
                    bar.update(idx)
                    if result is not None:
                        out.write(result)
                        out.flush()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        log.error("Provide the campaign number as the first and only argument.")
        sys.exit(1)
    else:
        if sys.argv[1] == "all":
            campaigns = range(0, 6)
        else:
            campaigns = [int(sys.argv[1])]

        for campaign in campaigns:
            log.info("Now indexing campaign {}".format(campaign))
            input_fn = "intermediate-data/k2-c{:02d}-tpf-urls.txt".format(campaign)
            output_fn = "intermediate-data/k2-c{:02d}-tpf-metadata.csv".format(campaign)
            write_metadata_table(input_fn, output_fn)
