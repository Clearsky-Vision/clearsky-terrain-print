# ClearSKY Terrain Print

Pick an area on the map and turn it into a 3D-printable terrain puzzle. A DEM gives the tiles their shape. A ClearSKY time series gives them their colors. You can use four colors per tile and change the spool set between tiles on the Flashforge Creator 5 Pro.

## Try Mount Fuji

1. Download `toposaic-clearsky-studio-windows.zip` from this repo's **Releases** and extract it. Install 64-bit Python 3.13 with Tcl/Tk and the `py` launcher if you need it.
2. Run **Start TopoSaic Studio.cmd**. The first run downloads the imagery libraries. Keep the launcher window open while you work.
3. In the browser, choose **ClearSKY studio**, **Load Fuji example**, and **Build print files**. The example includes three Sentinel-2 dates and a Copernicus DEM and works without a ClearSKY account.
4. Preview the four puzzle tiles and click **Download print kit**. Open the 3MFs in Flash Studio, choose the Creator 5 Pro profile, and map the colors to your loaded spools.

## Print your own area

1. Draw your area on the map and pick how many rows and columns you want.
2. Open **ClearSKY order** and enter your order UUID and API key. Click **Find dates**, select the dates you want, and review the processing-unit estimate before confirming the download. You can also import dated RGB GeoTIFFs under **Local series**.
3. Pick an elevation GeoTIFF and a median or mean composite. Set the tile width and pick your filament colors. Use one set for the whole print or assign colors tile by tile.
4. Build and download the kit. It includes the 3MF and STL files, a tile layout, and a filament guide.

We’ve generated and sliced the Fuji tiles. We still need to check the joint fit on the printer and try it with a live ClearSKY order.

[Detailed setup and printing guide](clearsky_print/README.md)

## Credits

Built on TopoSaic by Yann Ramin and contributors. See the [upstream docs](https://github.com/theatrus/toposaic/blob/fb365e0d12cfd99483d0d0cfe37077295d555b68/README.md), [Apache 2.0 license](LICENSE), [NOTICE](NOTICE), and [third-party credits](THIRD_PARTY_NOTICES.md). Keep the imagery and elevation credits from the print manifest with anything you share.
