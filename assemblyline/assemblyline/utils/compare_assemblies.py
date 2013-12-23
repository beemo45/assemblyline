'''
Created on Oct 8, 2012

@author: mkiyer

AssemblyLine: transcriptome meta-assembly from RNA-Seq

Copyright (C) 2012 Matthew Iyer

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
import argparse
import logging
import os
import sys
import operator
import collections
import shutil

import assemblyline
from assemblyline.lib.bx.intersection import Interval, IntervalTree
from assemblyline.lib.bx.cluster import ClusterTree
from assemblyline.lib.base import Category, GTFAttr
from assemblyline.lib.gtf import GTFFeature, sort_gtf
from assemblyline.lib.transcript import cmp_strand, parse_gtf, strand_int_to_str
from assemblyline.lib.assemble.transcript_graph import \
    find_exon_boundaries, split_exons

# for nearest transcripts calculation
MAX_LOCUS_DIST = 100000000

class Match(object):
    def __init__(self):
        self.nodes = collections.defaultdict(lambda: [])
        self.introns = []
        self.splicing = False

class MatchStats(object):
    @staticmethod
    def header_fields():
        return ['transcript_id', 'gene_id', 'locus', 'length', 'num_introns',
                'ref_transcript_id', 'ref_gene_id', 'ref_orig_gene_id', 'ref_gene_name', 
                'ref_source', 'ref_gene_type', 'ref_locus', 
                'ref_length', 'ref_num_introns',
                'shared_same_strand_bp', 'shared_opp_strand_bp',
                'shared_introns', 'shared_splicing',
                'distance', 'category']

    def __init__(self):
        for field in MatchStats.header_fields():
            setattr(self, field, None)

    def __str__(self):
        fields = []
        for field in MatchStats.header_fields():
            fields.append(getattr(self, field))
        return '\t'.join(map(str,fields))

    def add_gtf_attributes(self, feature):
        attrs = ['ref_transcript_id', 'ref_gene_id', 'ref_orig_gene_id', 
                 'ref_gene_name', 'ref_source', 'ref_gene_type', 
                 'ref_locus', 'ref_length', 'ref_num_introns',
                 'shared_same_strand_bp', 'shared_opp_strand_bp',
                 'shared_introns', 'shared_splicing',
                 'distance', 'category']
        for attr in attrs:
            v = getattr(self, attr)
            feature.attrs[attr] = v
    
    @staticmethod
    def from_transcript(t, ref=None):
        self = MatchStats()
        self.transcript_id = t.attrs[GTFAttr.TRANSCRIPT_ID]
        self.gene_id = t.attrs[GTFAttr.GENE_ID]
        self.locus = '%s:%d-%d[%s]' % (t.chrom, t.start, t.end, strand_int_to_str(t.strand))
        self.length = t.length
        self.num_introns = len(t.exons) - 1
        if ref is not None:
            self.ref_transcript_id = ref.attrs[GTFAttr.TRANSCRIPT_ID]
            self.ref_gene_id = ref.attrs[GTFAttr.GENE_ID]
            self.ref_orig_gene_id = ref.attrs.get('orig_gene_id', self.ref_gene_id)
            self.ref_gene_name = ref.attrs['gene_name']
            self.ref_source = ref.attrs['source']
            self.ref_gene_type = ref.attrs['gene_type']
            self.ref_locus = '%s:%d-%d[%s]' % (ref.chrom, ref.start, ref.end, strand_int_to_str(ref.strand))
            self.ref_length = ref.length
            self.ref_num_introns = len(ref.exons) - 1
        return self

    @staticmethod
    def choose_best(lst):
        if len(lst) == 0:
            return None
        hits = []
        same_strand_gene_ids = set()
        for m in lst:
            total_introns = m.num_introns + m.ref_num_introns
            if total_introns == 0:
                intron_frac = 0.0
            else:
                intron_frac = float(m.shared_introns) / (total_introns - m.shared_introns)
            same_strand_frac = float(m.shared_same_strand_bp) / (m.length + m.ref_length - m.shared_same_strand_bp)
            opp_strand_frac = float(m.shared_opp_strand_bp) / (m.length + m.ref_length - m.shared_opp_strand_bp)
            hits.append((int(m.shared_splicing), intron_frac, 
                         same_strand_frac, opp_strand_frac, 
                         int(m.category == Category.INTRONIC_SAME_STRAND),
                         int(m.category == Category.INTRONIC_OPP_STRAND),                                 
                         int(m.category == Category.INTERLEAVING_SAME_STRAND),
                         int(m.category == Category.INTERLEAVING_OPP_STRAND),
                         int(m.category == Category.ENCOMPASSING_SAME_STRAND),
                         int(m.category == Category.ENCOMPASSING_OPP_STRAND),                                                         
                         int(m.category == Category.INTERGENIC),                                                         
                         -abs(m.distance), m))
            # check for read through transcripts enumerating the 
            # same strand gene ids
            if m.category == Category.SAME_STRAND:
                same_strand_gene_ids.add(m.ref_gene_id)
        # sort matches
        hits.sort(reverse=True)
        hit = hits[0][-1]
        if (hit.category == Category.SAME_STRAND) and (len(same_strand_gene_ids) > 1):
            hit.category = Category.READ_THROUGH
        return hit

def compare_locus(transcripts):
    # store reference introns
    # (strand,start,end) -> ids (set) 
    ref_intron_dict = collections.defaultdict(lambda: [])
    ref_node_dict = collections.defaultdict(lambda: [])
    ref_splicing_patterns = collections.defaultdict(lambda: [])
    ref_dict = {}
    # find the intron domains of the transcripts
    boundaries = find_exon_boundaries(transcripts)
    test_transcripts = []
    for t in transcripts:
        # separate ref and nonref transcripts
        is_ref = bool(int(t.attrs[GTFAttr.REF]))
        if is_ref:
            # add to dict
            ref_id = t.attrs[GTFAttr.TRANSCRIPT_ID]
            ref_dict[ref_id] = t
            # split exons that cross boundaries and get the
            # nodes in the transcript path
            for n in split_exons(t, boundaries):
                ref_node_dict[n].append(t)
            # add to introns
            splicing_pattern = []
            for start,end in t.iterintrons():
                intron = (t.strand, start, end)
                ref_intron_dict[intron].append(t)
                splicing_pattern.append(intron)
            # add to splicing patterns
            if len(splicing_pattern) > 0:
                ref_splicing_patterns[tuple(splicing_pattern)].append(t)
        else:
            test_transcripts.append(t)
    # index introns for fast intersection
    intron_tree = IntervalTree()
    for intron, refs in ref_intron_dict.iteritems():
        strand, start, end = intron
        intron_tree.insert_interval(Interval(start,end,strand=strand,value=refs))
    # categorize transcripts
    for t in test_transcripts:
        # get transcript nodes and introns
        nodes = list(split_exons(t, boundaries))
        introns = []
        for start,end in t.iterintrons():
            introns.append((t.strand,start,end))
        splicing_pattern = tuple(introns)
        # keep list of all matching ref transcripts
        matches = collections.defaultdict(lambda: Match())
        # dict of reference transcripts -> category -> list of nodes
        for n in nodes:
            if n in ref_node_dict:
                # look for reference transcripts that share this node
                for ref in ref_node_dict[n]:
                    if cmp_strand(t.strand, ref.strand):
                        c = Category.SAME_STRAND
                    else:
                        c = Category.OPP_STRAND
                    ref_id = ref.attrs[GTFAttr.TRANSCRIPT_ID]
                    m = matches[ref_id]
                    m.nodes[c].append(n)
            # look for reference introns that overlap this node
            for hit in intron_tree.find(*n):
                if cmp_strand(t.strand, hit.strand):
                    c = Category.INTRONIC_SAME_STRAND
                else:
                    c = Category.INTRONIC_OPP_STRAND
                for ref in hit.value: 
                    ref_id = ref.attrs[GTFAttr.TRANSCRIPT_ID]
                    m = matches[ref_id]
                    m.nodes[c].append(n)          
        # dict of introns -> list of reference transcripts
        for intron in introns:
            if intron in ref_intron_dict:
                for ref in ref_intron_dict[intron]:
                    ref_id = ref.attrs[GTFAttr.TRANSCRIPT_ID]
                    m = matches[ref_id]
                    m.introns.append(intron)
        # check splicing pattern matches
        if len(splicing_pattern) > 0:
            if splicing_pattern in ref_splicing_patterns:
                for ref in ref_splicing_patterns[splicing_pattern]:
                    ref_id = ref.attrs[GTFAttr.TRANSCRIPT_ID]
                    m = matches[ref_id]
                    m.splicing = True
        # go through the matches for this transcript and determine
        # the transcript category
        match_stats = []
        for ref_id, m in matches.iteritems():
            ref = ref_dict[ref_id]
            # calculate coverage
            same_strand_bp = sum((n[1] - n[0]) for n in m.nodes[Category.SAME_STRAND])
            opp_strand_bp = sum((n[1] - n[0]) for n in m.nodes[Category.OPP_STRAND])
            # count shared introns
            num_shared_introns = len(m.introns)
            # decide category for this test/ref transcript pair
            if m.splicing or (num_shared_introns > 0) or (same_strand_bp > 0):
                c = Category.SAME_STRAND
            elif (opp_strand_bp > 0):
                c = Category.OPP_STRAND
            else:
                # count nodes of different types
                num_same_strand = len(m.nodes[Category.SAME_STRAND])
                num_opp_strand = len(m.nodes[Category.OPP_STRAND])
                num_intronic_same_strand = len(m.nodes[Category.INTRONIC_SAME_STRAND])
                num_intronic_opp_strand = len(m.nodes[Category.INTRONIC_OPP_STRAND])
                assert num_same_strand == 0
                assert num_opp_strand == 0
                num_intronic = (num_intronic_same_strand +
                                num_intronic_opp_strand)
                #print t.attrs[GTFAttr.TRANSCRIPT_ID], ref_id, 'num_nodes', len(nodes), 'same strand', num_same_strand, 'opp', num_opp_strand, 'intronic same s', num_intronic_same_strand, 'intronic opp', num_intronic_opp_strand
                #print t.attrs[GTFAttr.TRANSCRIPT_ID], ref_id, 'nodes', nodes
                assert num_intronic > 0
                if (num_intronic == len(nodes)):
                    # completely intronic
                    if num_intronic_same_strand > 0:
                        c = Category.INTRONIC_SAME_STRAND
                    else:
                        c = Category.INTRONIC_OPP_STRAND
                else:
                    # interleaving means some nodes intronic and other intergenic
                    if num_intronic_same_strand > 0:
                        c = Category.INTERLEAVING_SAME_STRAND
                    else:
                        c = Category.INTERLEAVING_OPP_STRAND
            # create a match object
            ms = MatchStats.from_transcript(t, ref)
            ms.shared_same_strand_bp = same_strand_bp
            ms.shared_opp_strand_bp = opp_strand_bp
            ms.shared_introns = num_shared_introns
            ms.shared_splicing = m.splicing
            ms.category = Category.to_str(c)            
            ms.distance = 0
            match_stats.append(ms)
        # choose the best match
        best_match = MatchStats.choose_best(match_stats)
        yield (t, best_match, match_stats)

class LocusFeature(object):
    def __init__(self):
        self.chrom = None
        self.start = None
        self.end = None
        self.strand = None
        self.transcript_id = None
        self.gene_id = None
        self.orig_gene_id = None
        self.gene_name = None
        self.source = None
        self.gene_type = None
        self.length = None
        self.num_introns = None

def build_locus_trees(gtf_file):
    features = []
    locus_cluster_trees = collections.defaultdict(lambda: ClusterTree(0,1))    
    for locus_transcripts in parse_gtf(open(gtf_file)):
        for t in locus_transcripts: 
            is_ref = bool(int(t.attrs[GTFAttr.REF]))
            if not is_ref:
                continue
            # build locus feature
            f = LocusFeature()
            f.transcript_id = t.attrs[GTFAttr.TRANSCRIPT_ID]
            f.gene_id = t.attrs[GTFAttr.GENE_ID]
            f.orig_gene_id = t.attrs.get('orig_gene_id', f.gene_id)
            f.gene_name = t.attrs['gene_name']
            f.source = t.attrs['source']
            f.gene_type = t.attrs['gene_type']
            f.chrom = t.chrom
            f.start = t.start
            f.end = t.end
            f.strand = t.strand
            f.length = t.length
            f.num_introns = len(t.exons) - 1
            i = len(features)
            features.append(f)
            locus_cluster_trees[t.chrom].insert(t.start, t.end, i)
    # build interval trees of loci
    locus_trees = collections.defaultdict(lambda: IntervalTree())
    for chrom, cluster_tree in locus_cluster_trees.iteritems():
        for locus_start, locus_end, indexes in cluster_tree.getregions():
            for i in indexes:
                locus_features = [features[i] for i in indexes]
                locus_trees[chrom].insert_interval(Interval(locus_start, locus_end, value=locus_features))
    return locus_trees

def find_nearest_transcripts(chrom, start, end, strand, locus_trees):
    # first check for overlap
    nearest_features = []
    hits = locus_trees[chrom].find(start, end)
    for hit in hits:
        for f in hit.value:
            if cmp_strand(f.strand, strand):
                c = Category.ENCOMPASSING_SAME_STRAND
            else:
                c = Category.ENCOMPASSING_OPP_STRAND
            nearest_features.append((f, c, 0))
    # look left and right
    left_hits = locus_trees[chrom].before(start, num_intervals=1, max_dist=MAX_LOCUS_DIST)        
    right_hits = locus_trees[chrom].after(end, num_intervals=1, max_dist=MAX_LOCUS_DIST)
    # look for nearest hit
    for hits in (left_hits, right_hits):
        nearest_locus_hit = None
        nearest_dist = MAX_LOCUS_DIST
        for hit in hits:
            dist = min(abs(start - hit.end), abs(hit.start - end))
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_locus_hit = hit
        if nearest_locus_hit is not None:
            for f in nearest_locus_hit.value:
                dist = min(abs(start - f.end), abs(f.start - end))
                nearest_features.append((f, Category.INTERGENIC, dist))
    return nearest_features    

def _parse_gtf_by_chrom(gtf_file):
    current_chrom = None
    exon_dict = collections.defaultdict(lambda: [])
    transcript_dict = {}
    for feature in GTFFeature.parse(open(gtf_file)):
        if (current_chrom != feature.seqid):
            if len(exon_dict) > 0:
                yield current_chrom, transcript_dict, exon_dict
                exon_dict = collections.defaultdict(lambda: [])
                transcript_dict = {}
            current_chrom = feature.seqid
        t_id = feature.attrs[GTFAttr.TRANSCRIPT_ID]
        if feature.feature_type == "transcript":
            transcript_dict[t_id] = feature
        elif feature.feature_type == "exon":
            exon_dict[t_id].append(feature)
    if len(exon_dict) > 0:
        yield current_chrom, transcript_dict, exon_dict

def add_gtf_file(gtf_file, outfh, is_ref):
    refval = '1' if is_ref else '0'
    for chrom, transcript_dict, exon_dict in _parse_gtf_by_chrom(gtf_file):
        logging.debug("\tfinished chrom %s %d features" % (chrom, len(exon_dict)))
        # output reference transcripts
        for t_id, features in exon_dict.iteritems():
            # sort features (exons) by start position
            features.sort(key=operator.attrgetter('start'))
            # annotate exons as reference features
            for f in features:
                f.attrs[GTFAttr.REF] = refval
                print >>outfh, str(f)
            # transcript feature
            if t_id in transcript_dict:
                f = transcript_dict[t_id]
            else:
                f = GTFFeature()
                f.seqid = features[0].seqid
                f.source = features[0].source
                f.feature_type = 'transcript'
                f.start = features[0].start
                f.end = features[-1].end
                f.score = features[0].score
                f.strand = features[0].strand
                f.phase = '.'
                f.attrs = features[0].attrs.copy()
                if "exon_number" in f.attrs:
                    del f.attrs["exon_number"]
            f.attrs[GTFAttr.REF] = refval
            print >>outfh, str(f)

def compare_assemblies(ref_gtf_file, test_gtf_file, output_dir): 
    # output files
    if not os.path.exists(output_dir):
        logging.info('Creating output dir: %s' % (output_dir))
        os.makedirs(output_dir)
    # merge step
    merged_gtf_file = os.path.join(output_dir, "merged.gtf")
    merged_sorted_gtf_file = os.path.splitext(merged_gtf_file)[0] + ".srt.gtf"
    merge_done_file = os.path.join(output_dir, 'merged.done')
    sort_done_file = os.path.join(output_dir, 'sort.done')
    if not os.path.exists(merge_done_file):
        # merge and sort ref/test gtf files
        logging.info("Merging reference and test GTF files")
        # make temporary file to store merged ref/test gtf files
        with open(merged_gtf_file, "w") as fileh:
            logging.info("Adding reference GTF file")
            add_gtf_file(ref_gtf_file, fileh, is_ref=True)
            logging.info("Adding test GTF file")
            add_gtf_file(test_gtf_file, fileh, is_ref=False)
        open(merge_done_file, 'w').close()
    if not os.path.exists(sort_done_file):        
        logging.info("Sorting merged GTF file")
        # create temp directory
        tmp_dir = os.path.join(output_dir, 'tmp')    
        if not os.path.exists(tmp_dir):
            logging.debug("Creating tmp directory '%s'" % (tmp_dir))
            os.makedirs(tmp_dir)
        sort_gtf(merged_gtf_file, merged_sorted_gtf_file, tmp_dir=tmp_dir)
        # cleanup
        shutil.rmtree(tmp_dir)
        open(sort_done_file, 'w').close()
    # compare assemblies
    overlapping_gtf_file = os.path.join(output_dir, 'overlapping.gtf')
    intergenic_tmp_gtf_file = os.path.join(output_dir, 'intergenic.tmp.gtf')
    overlapping_file = os.path.join(output_dir, 'overlapping.tsv')
    overlapping_best_file = os.path.join(output_dir, 'overlapping.best.tsv')
    overlapping_done_file = os.path.join(output_dir, 'overlapping.done')
    if not os.path.exists(overlapping_done_file):
        logging.info("Comparing assemblies")
        gtf_fileh = open(overlapping_gtf_file, 'w')
        tmp_gtf_fileh = open(intergenic_tmp_gtf_file, 'w')
        overlapping_fileh = open(overlapping_file, 'w')
        overlapping_best_fileh = open(overlapping_best_file, 'w')
        for locus_transcripts in parse_gtf(open(merged_sorted_gtf_file)):
            locus_chrom = locus_transcripts[0].chrom
            locus_start = locus_transcripts[0].start
            locus_end = max(t.end for t in locus_transcripts)
            logging.debug("[LOCUS] %s:%d-%d %d transcripts" % 
                          (locus_chrom, locus_start, locus_end, 
                           len(locus_transcripts)))
            for t, best_match, match_stats in compare_locus(locus_transcripts):
                features = t.to_gtf_features(source='assembly')
                if len(match_stats) == 0:
                    # write intergenic transcripts to analyze separately
                    for f in features:
                        print >>tmp_gtf_fileh, str(f)
                else:
                    assert best_match is not None
                    # add gtf attributes and write
                    for f in features:
                        best_match.add_gtf_attributes(f)
                        print >>gtf_fileh, str(f)
                    # tab-delimited text output
                    print >>overlapping_best_fileh, str(best_match)
                    for ms in match_stats:
                        print >>overlapping_fileh, str(ms)
        gtf_fileh.close()
        tmp_gtf_fileh.close()
        overlapping_fileh.close()
        overlapping_best_fileh.close()
        open(overlapping_done_file, 'w').close()
    # resolve intergenic transcripts
    intergenic_gtf_file = os.path.join(output_dir, 'intergenic.gtf')
    intergenic_file = os.path.join(output_dir, 'intergenic.tsv')
    intergenic_best_file = os.path.join(output_dir, 'intergenic.best.tsv')
    intergenic_done_file = os.path.join(output_dir, 'intergenic.done')
    if not os.path.exists(intergenic_done_file):
        logging.info("Building interval index")
        locus_trees = build_locus_trees(merged_sorted_gtf_file)
        logging.info('Finding nearest matches to intergenic transcripts')
        gtf_fileh = open(intergenic_gtf_file, 'w')
        intergenic_fileh = open(intergenic_file, 'w')
        intergenic_best_fileh = open(intergenic_best_file, 'w')
        for locus_transcripts in parse_gtf(open(intergenic_tmp_gtf_file)):
            for t in locus_transcripts:
                # find nearest transcripts
                nearest_transcripts = find_nearest_transcripts(t.chrom, t.start, t.end, t.strand, locus_trees)
                match_stats = []
                best_match = None
                if len(nearest_transcripts) == 0:
                    best_match = MatchStats.from_transcript(t)
                    best_match.category = Category.to_str(Category.INTERGENIC)
                    match_stats.append(best_match)
                else:
                    for feature,category,dist in nearest_transcripts: 
                        # create a match object
                        ms = MatchStats.from_transcript(t)
                        ms.ref_transcript_id = feature.transcript_id
                        ms.ref_gene_id = feature.gene_id
                        ms.ref_orig_gene_id = feature.orig_gene_id
                        ms.ref_gene_name = feature.gene_name
                        ms.ref_source = feature.source
                        ms.ref_gene_type = feature.gene_type
                        ms.ref_length = feature.length
                        ms.ref_locus = '%s:%d-%d[%s]' % (feature.chrom, feature.start, feature.end, strand_int_to_str(feature.strand))
                        ms.ref_num_introns = feature.num_introns
                        ms.shared_same_strand_bp = 0
                        ms.shared_opp_strand_bp = 0
                        ms.shared_introns = 0
                        ms.shared_splicing = False
                        ms.category = Category.to_str(category)
                        ms.distance = dist
                        match_stats.append(ms)
                    # choose the best match
                    best_match = MatchStats.choose_best(match_stats)
                # add gtf attributes and write
                for f in t.to_gtf_features(source='assembly'):
                    best_match.add_gtf_attributes(f)
                    print >>gtf_fileh, str(f)
                # write tab-delimited data
                print >>intergenic_best_fileh, str(best_match)
                for ms in match_stats:
                    print >>intergenic_fileh, str(ms)                    
        gtf_fileh.close()
        intergenic_fileh.close()
        intergenic_best_fileh.close()
        open(intergenic_done_file, 'w').close()
    # merge overlapping and intergenic results
    logging.info('Merging results')
    metadata_file = os.path.join(output_dir, 'metadata.txt')
    metadata_best_file = os.path.join(output_dir, 'metadata.best.txt')
    assembly_gtf_file = os.path.join(output_dir, 'assembly.cmp.gtf')
    combine_done_file = os.path.join(output_dir, 'done')
    if not os.path.exists(combine_done_file):
        filenames = [overlapping_file, intergenic_file]
        with open(metadata_file, 'w') as outfile:
            print >>outfile, '\t'.join(MatchStats.header_fields())
            for fname in filenames:
                with open(fname) as infile:
                    for line in infile:
                        outfile.write(line)
        filenames = [overlapping_best_file, intergenic_best_file]
        with open(metadata_best_file, 'w') as outfile:
            print >>outfile, '\t'.join(MatchStats.header_fields())
            for fname in filenames:
                with open(fname) as infile:
                    for line in infile:
                        outfile.write(line)
        filenames = [intergenic_gtf_file, overlapping_gtf_file]
        with open(assembly_gtf_file, 'w') as outfile:
            for fname in filenames:
                with open(fname) as infile:
                    for line in infile:
                        outfile.write(line)
        open(combine_done_file, 'w').close()
    # cleanup
    logging.info("Done")

def main():
    # parse command line
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", 
                        dest="verbose", default=False)
    parser.add_argument("-o", "--output-dir", dest="output_dir", 
                        default="compare")
    parser.add_argument("ref_gtf_file")
    parser.add_argument("test_gtf_file")
    args = parser.parse_args()
    # set logging level
    if args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # check command line parameters
    if not os.path.exists(args.ref_gtf_file):
        parser.error("Reference GTF file %s not found" % (args.ref_gtf_file))
    if not os.path.exists(args.test_gtf_file):
        parser.error("Test GTF file %s not found" % (args.test_gtf_file))
    if not os.path.exists(args.output_dir):
        logging.debug("Creating output directory '%s'" % (args.output_dir))
        os.makedirs(args.output_dir)
    logging.info("AssemblyLine %s" % (assemblyline.__version__))
    logging.info("----------------------------------")   
    # show parameters
    logging.info("Parameters:")
    logging.info("verbose logging:       %s" % (args.verbose))
    logging.info("reference gtf file:    %s" % (args.ref_gtf_file))
    logging.info("test gtf file:         %s" % (args.test_gtf_file))
    logging.info("output dir:            %s" % (args.output_dir))
    compare_assemblies(args.ref_gtf_file, args.test_gtf_file, 
                       args.output_dir)
    return 0

if __name__ == "__main__":
    sys.exit(main())