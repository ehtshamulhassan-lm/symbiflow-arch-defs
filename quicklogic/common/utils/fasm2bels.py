import argparse
import pickle
import re
from collections import defaultdict, namedtuple
import fasm

from connections import get_name_and_hop

from pathlib import Path
from data_structs import Loc, SwitchboxPinLoc, PinDirection, Tile
from verilogmodule import VModule

from quicklogic_fasm.qlfasm import QL732BAssembler, load_quicklogic_database

Feature = namedtuple('Feature', 'loc typ signature value')
RouteEntry = namedtuple('RouteEntry', 'typ stage_id switch_id mux_id sel_id')
MultiLocCellMapping = namedtuple(
    'MultiLocCellMapping', 'typ fromlocset toloc pinnames'
)


class Fasm2Bels(object):
    '''Class for parsing FASM file and producing BEL representation.

    It takes FASM lines and VPR database and converts the data to Basic
    Elements and connections between them. It allows converting this data to
    Verilog.
    '''

    class Fasm2BelsException(Exception):
        '''Exception for Fasm2Bels errors and unsupported features.
        '''

        def __init__(self, message):
            self.message = message

        def __str__(self):
            return self.message

    def __init__(self, vpr_db, package_name):
        '''Prepares required structures for converting FASM to BELs.

        Parameters
        ----------
        vpr_db: dict
            A dictionary containing cell_library, loc_map, vpr_tile_types,
            vpr_tile_grid, vpr_switchbox_types, vpr_switchbox_grid,
            connections, vpr_package_pinmaps
        '''

        # load vpr_db data
        self.cells_library = vpr_db["cells_library"]
        #self.loc_map = db["loc_map"]
        self.vpr_tile_types = vpr_db["tile_types"]
        self.vpr_tile_grid = vpr_db["phy_tile_grid"]
        self.vpr_switchbox_types = vpr_db["switchbox_types"]
        self.vpr_switchbox_grid = vpr_db["switchbox_grid"]
        self.connections = vpr_db["connections"]
        self.package_name = package_name

        self.io_to_fbio = dict()

        for name, package in db['package_pinmaps'][self.package_name].items():
            self.io_to_fbio[package[0].loc] = name

        # Add ASSP to all locations it covers
        # TODO maybe this should be added in original vpr_tile_grid
        # set all cels in row 1 and column 2 to ASSP
        # In VPR grid, the ASSP tile is located in (1, 1)
        numassp = 1
        assplocs = set()
        ramlocs = dict()
        multlocs = dict()

        assp_tile = self.vpr_tile_grid[Loc(1, 1)]
        assp_cell = assp_tile.cells[0]
        for phy_loc, tile in self.vpr_tile_grid.items():
            tile_type = self.vpr_tile_types[tile.type]
            if "ASSP" in tile_type.cells:
                assplocs.add(phy_loc)

            if "RAM" in tile_type.cells:
                ramcell = [cell for cell in tile.cells if cell.type == "RAM"]
                cellname = ramcell[0].name
                if cellname not in ramlocs:
                    ramlocs[cellname] = set()

                ramlocs[cellname].add(phy_loc)

            if "MULT" in tile_type.cells:
                multcell = [cell for cell in tile.cells if cell.type == "MULT"]
                cellname = multcell[0].name
                if cellname not in multlocs:
                    multlocs[cellname] = set()

                multlocs[cellname].add(phy_loc)

        # this map represents the mapping from input name to its inverter name
        self.inversionpins = {
            'LOGIC':
                {
                    'TA1': 'TAS1',
                    'TA2': 'TAS2',
                    'TB1': 'TBS1',
                    'TB2': 'TBS2',
                    'BA1': 'BAS1',
                    'BA2': 'BAS2',
                    'BB1': 'BBS1',
                    'BB2': 'BBS2',
                    'QCK': 'QCKS'
                }
        }

        # prepare helper structure for connections
        self.connections_by_loc = defaultdict(list)
        for connection in self.connections:
            self.connections_by_loc[connection.dst].append(connection)
            self.connections_by_loc[connection.src].append(connection)

        # a mapping from the type of cell FASM line refers to to its parser
        self.featureparsers = {
            'LOGIC': self.parse_logic_line,
            'QMUX': self.parse_logic_line,
            'GMUX': self.parse_logic_line,
            'INTERFACE': self.parse_interface_line,
            'ROUTING': self.parse_routing_line
        }

        # a mapping from cell type to a set of possible pin names
        self.pinnames = defaultdict(set)
        for celltype in self.cells_library.values():
            typ = celltype.type
            for pin in celltype.pins:
                self.pinnames[typ].add(pin.name)

        # a mapping from cell types that occupy multiple locations
        # to a single location
        self.multiloccells = {
            'ASSP':
                MultiLocCellMapping(
                    'ASSP', assplocs, Loc(1, 1), self.pinnames['ASSP']
                )
        }
        for ram in ramlocs:
            self.multiloccells[ram] = MultiLocCellMapping(
                ram, ramlocs[ram],
                list(ramlocs[ram])[0], self.pinnames['RAM']
            )
        for mult in multlocs:
            self.multiloccells[mult] = MultiLocCellMapping(
                mult, multlocs[mult],
                list(multlocs[mult])[1], self.pinnames['MULT']
            )

        # helper routing data
        self.routingdata = defaultdict(list)
        # a dictionary holding bit settings for BELs
        self.belinversions = defaultdict(lambda: defaultdict(list))
        # a dictionary holding bit settings for IOs
        self.interfaces = defaultdict(lambda: defaultdict(list))
        # a dictionary holding simplified connections between BELs
        self.designconnections = defaultdict(dict)
        # a dictionary holding hops from routing
        self.designhops = defaultdict(dict)

    def parse_logic_line(self, feature: Feature):
        '''Parses a setting for a BEL.

        Parameters
        ----------
        feature: Feature
            FASM line for BEL
        '''
        belname, setting = feature.signature.split('.', 1)
        if feature.value == 1:
            # FIXME handle ZINV pins
            if 'ZINV.' in setting:
                setting = setting.replace('ZINV.', '')
            elif 'INV.' in setting:
                setting = setting.replace('INV.', '')
            self.belinversions[feature.loc][belname].append(setting)

    def parse_interface_line(self, feature: Feature):
        '''Parses a setting for IO.

        Parameters
        ----------
        feature: Feature
            FASM line for BEL
        '''
        belname, setting = feature.signature.split('.', 1)
        if feature.value == 1:
            setting = setting.replace('ZINV.', '')
            setting = setting.replace('INV.', '')
            self.interfaces[feature.loc][belname].append(setting)

    def parse_routing_line(self, feature: Feature):
        '''Parses a routing setting.

        Parameters
        ----------
        feature: Feature
            FASM line for BEL
        '''
        match = re.match(
            r'^I_highway\.IM(?P<switch_id>[0-9]+)\.I_pg(?P<sel_id>[0-9]+)$',
            feature.signature
        )
        if match:
            typ = 'HIGHWAY'
            stage_id = 3  # FIXME: Get HIGHWAY stage id from the switchbox def
            switch_id = int(match.group('switch_id'))
            mux_id = 0
            sel_id = int(match.group('sel_id'))
        match = re.match(
            r'^I_street\.Isb(?P<stage_id>[0-9])(?P<switch_id>[0-9])\.I_M(?P<mux_id>[0-9]+)\.I_pg(?P<sel_id>[0-9]+)$',  # noqa: E501
            feature.signature
        )
        if match:
            typ = 'STREET'
            stage_id = int(match.group('stage_id')) - 1
            switch_id = int(match.group('switch_id')) - 1
            mux_id = int(match.group('mux_id'))
            sel_id = int(match.group('sel_id'))
        self.routingdata[feature.loc].append(
            RouteEntry(
                typ=typ,
                stage_id=stage_id,
                switch_id=switch_id,
                mux_id=mux_id,
                sel_id=sel_id
            )
        )

    def parse_fasm_lines(self, fasmlines):
        '''Parses FASM lines.

        Parameters
        ----------
        fasmlines: list
            A list of FasmLine objects
        '''

        loctyp = re.compile(
            r'^X(?P<x>[0-9]+)Y(?P<y>[0-9]+)\.(?P<type>[A-Z]+)\.(?P<signature>.*)$'
        )  # noqa: E501

        for line in fasmlines:
            if not line.set_feature:
                continue
            match = loctyp.match(line.set_feature.feature)
            if not match:
                raise self.Fasm2BelsException(
                    f'FASM features have unsupported format:  {line.set_feature}'
                )  # noqa: E501
            loc = Loc(x=int(match.group('x')), y=int(match.group('y')))
            typ = match.group('type')
            feature = Feature(
                loc=loc,
                typ=typ,
                signature=match.group('signature'),
                value=line.set_feature.value
            )
            self.featureparsers[typ](feature)

    def decode_switchbox(self, switchbox, features):
        '''Decodes all switchboxes to extract full connections' info.

        For every output, this method determines its input in the routing
        switchboxes. In this representation, an input and output can be either
        directly connected to a BEL, or to a hop wire.

        Parameters
        ----------
        switchbox: a Switchbox object from vpr_switchbox_types
        features: features regarding given switchbox

        Returns
        -------
        dict: a mapping from output pin to input pin for a given switchbox
        '''
        # Group switchbox connections by destinationa
        conn_by_dst = defaultdict(set)
        for c in switchbox.connections:
            conn_by_dst[c.dst].add(c)

        # Prepare data structure
        mux_sel = {}
        for stage_id, stage in switchbox.stages.items():
            mux_sel[stage_id] = {}
            for switch_id, switch in stage.switches.items():
                mux_sel[stage_id][switch_id] = {}
                for mux_id, mux in switch.muxes.items():
                    mux_sel[stage_id][switch_id][mux_id] = None

        for feature in features:
            assert mux_sel[feature.stage_id][feature.switch_id][
                feature.mux_id] is None, feature  # noqa: E501
            mux_sel[feature.stage_id][feature.switch_id][
                feature.mux_id] = feature.sel_id  # noqa: E501

        def expand_mux(out_loc):
            """
            Expands a multiplexer output until a switchbox input is reached.
            Returns name of the input or None if not found.

            Parameters
            ----------
            out_loc: the last output location

            Returns
            -------
            str: None if input name not found, else string
            """

            # Get mux selection, If it is set to None then the mux is
            # not active
            sel = mux_sel[out_loc.stage_id][out_loc.switch_id][out_loc.mux_id]
            if sel is None:
                return None  # TODO can we return None?

            stage = switchbox.stages[out_loc.stage_id]
            switch = stage.switches[out_loc.switch_id]
            mux = switch.muxes[out_loc.mux_id]
            pin = mux.inputs[sel]

            if pin.name is not None:
                return pin.name

            inp_loc = SwitchboxPinLoc(
                stage_id=out_loc.stage_id,
                switch_id=out_loc.switch_id,
                mux_id=out_loc.mux_id,
                pin_id=sel,
                pin_direction=PinDirection.INPUT
            )

            # Expand all "upstream" muxes that connect to the selected
            # input pin
            assert inp_loc in conn_by_dst, inp_loc
            for c in conn_by_dst[inp_loc]:
                inp = expand_mux(c.src)
                if inp is not None:
                    return inp

            # Nothing found
            return None  # TODO can we return None?

        # For each output pin of a switchbox determine to which input is it
        # connected to.
        routes = {}
        for out_pin in switchbox.outputs.values():
            out_loc = out_pin.locs[0]
            routes[out_pin.name] = expand_mux(out_loc)

        return routes

    def process_switchbox(self, loc, switchbox, features):
        '''Processes all switchboxes and extract hops from connections.

        The function extracts final connections from inputs to outputs, and
        hops into separate structures for further processing.

        Parameters
        ----------
        loc: Loc
            location of the current switchbox
        switchbox: Switchbox
            a switchbox
        features: list
            list of features regarding given switchbox
        '''
        routes = self.decode_switchbox(switchbox, features)
        for k, v in routes.items():
            if v is not None:
                if re.match('[VH][0-9][LRBT][0-9]', k):
                    self.designhops[(loc.x, loc.y)][k] = v
                else:
                    self.designconnections[loc][k] = v

    def resolve_hops(self):
        '''Resolves remaining hop wires.

        It determines the absolute input for the given pin by resolving hop
        wires and adds those final connections to the design connections.
        '''
        for loc, conns in self.designconnections.items():
            for pin, source in conns.items():
                hop = get_name_and_hop(source)
                tloc = loc
                while hop[1] is not None:
                    tloc = Loc(tloc[0] + hop[1][0], tloc[1] + hop[1][1])
                    # in some cases BEL is distanced from a switchbox, in those
                    # cases the hop will not point to another hop. We should
                    # simply return the pin here in the correct location
                    if hop[0] in self.designhops[tloc]:
                        hop = get_name_and_hop(self.designhops[tloc][hop[0]])
                    else:
                        hop = (hop[0], None)
                self.designconnections[loc][pin] = (tloc, hop[0])

    def resolve_connections(self):
        '''Resolves connections between BELs and IOs.
        '''
        keys = sorted(self.routingdata.keys(), key=lambda loc: (loc.x, loc.y))
        for loc in keys:
            routingfeatures = self.routingdata[loc]
            # map location to VPR coordinates
            #if phy_loc not in self.loc_map.fwd:
            #    continue
            #loc = self.loc_map.fwd[phy_loc]

            if loc in self.vpr_switchbox_grid:
                typ = self.vpr_switchbox_grid[loc]
                switchbox = self.vpr_switchbox_types[typ]
                self.process_switchbox(loc, switchbox, routingfeatures)
        self.resolve_hops()

    def remap_multiloc_loc(self, loc, pinname=None, celltype=None):
        '''Unifies coordinates of cells occupying multiple locations.

        Some cells, like ASSP, RAM or multipliers occupy multiple locations.
        This method groups bits and connections for those cells into a single
        artificial location.

        Parameters
        ----------
        loc: Loc
            The current location
        pinname: str
            The optional name of the pin (used to determine to which cell
            pin refers to)
        celltype: str
            The optional name of the cell type

        Returns
        -------
        Loc: the new location of the cell
        '''
        finloc = loc
        for multiloc in self.multiloccells.values():
            if pinname is None or pinname in multiloc.pinnames or celltype == multiloc.typ:
                if loc in multiloc.fromlocset:
                    finloc = multiloc.toloc
                    break
        return finloc

    def resolve_multiloc_cells(self):
        '''Groups cells that are scattered around multiple locations.
        '''
        newbelinversions = defaultdict(lambda: defaultdict(list))
        newdesignconnections = defaultdict(dict)

        for bellockey, bellocpair in self.belinversions.items():
            for belloctype, belloc in bellocpair.items():
                if belloctype in self.multiloccells:
                    newbelinversions[self.remap_multiloc_loc(
                        bellockey, celltype=belloctype
                    )][belloctype].extend(belloc)
        self.belinversion = newbelinversions
        for loc, conns in self.designconnections.items():
            for pin, src in conns.items():
                dstloc = self.remap_multiloc_loc(loc, pinname=pin)
                srcloc = self.remap_multiloc_loc(src[0], pinname=src[1])
                newdesignconnections[dstloc][pin] = (srcloc, src[1])
        self.designconnections = newdesignconnections

    def produce_verilog(self, pcf_data):
        '''Produces string containing Verilog module representing FASM.

        Returns
        -------
        str, str: a Verilog module and PCF
        '''
        module = VModule(
            self.vpr_tile_grid, self.vpr_tile_types, self.cells_library,
            pcf_data, self.belinversions, self.interfaces,
            self.designconnections, self.inversionpins, self.io_to_fbio
        )
        module.parse_bels()
        verilog = module.generate_verilog()
        pcf = module.generate_pcf()
        qcf = module.generate_qcf()
        return verilog, pcf, qcf

    def convert_to_verilog(self, fasmlines):
        '''Runs all methods required to convert FASM lines to Verilog module.

        Parameters
        ----------
        fasmlines: list
            FASM lines to process

        Returns
        -------
        str: a Verilog module
        '''
        self.parse_fasm_lines(fasmlines)
        self.resolve_connections()
        self.resolve_multiloc_cells()
        verilog, pcf, qcf = self.produce_verilog(pcf_data)
        return verilog, pcf, qcf


def parse_pcf(pcf):
    pcf_data = {}
    with open(pcf, 'r') as fp:
        for l in fp:
            line = l.strip().split()
            if len(line) != 3:
                continue
            if line[0] != 'set_io':
                continue
            pcf_data[line[2]] = line[1]
    return pcf_data


if __name__ == '__main__':
    # Parse arguments
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument("input_file", type=Path, help="Input fasm file")

    parser.add_argument(
        "--vpr-db", type=str, required=True, help="VPR database file"
    )

    parser.add_argument(
        "--package-name",
        type=str,
        required=True,
        choices=['PD64', 'PU64', 'WR42'],
        default='PD64',
        help="The package name"
    )

    parser.add_argument(
        "--input-type",
        type=str,
        choices=['bitstream', 'fasm'],
        default='fasm',
        help="Determines whether the input is a FASM file or bitstream"
    )

    parser.add_argument(
        "--output-verilog",
        type=Path,
        required=True,
        help="Output Verilog file"
    )
    parser.add_argument(
        "--input-pcf",
        type=Path,
        required=False,
        help=
        "Pins constraint file. If provided the tool will use the info to keep the original io pins names"
    )

    parser.add_argument("--output-pcf", type=Path, help="Output PCF file")

    parser.add_argument("--output-qcf", type=Path, help="Output QCF file")

    args = parser.parse_args()

    pcf_data = {}

    if args.input_pcf is not None:
        pcf_data = parse_pcf(args.input_pcf)

    # Load data from the database
    with open(args.vpr_db, "rb") as fp:
        db = pickle.load(fp)

    f2b = Fasm2Bels(db, args.package_name)

    if args.input_type == 'bitstream':
        qlfasmdb = load_quicklogic_database()
        assembler = QL732BAssembler(qlfasmdb)
        assembler.read_bitstream(args.input_file)
        fasmlines = assembler.disassemble()
        fasmlines = [
            line for line in fasm.parse_fasm_string('\n'.join(fasmlines))
        ]
    else:
        fasmlines = [
            line for line in fasm.parse_fasm_filename(args.input_file)
        ]

    verilog, pcf, qcf = f2b.convert_to_verilog(fasmlines)

    with open(args.output_verilog, 'w') as outv:
        outv.write(verilog)
    if args.output_pcf:
        with open(args.output_pcf, 'w') as outpcf:
            outpcf.write(pcf)
    if args.output_qcf:
        with open(args.output_qcf, 'w') as outqcf:
            outqcf.write(qcf)