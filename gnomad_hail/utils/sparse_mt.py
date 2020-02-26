from .generic import *

INFO_SUM_AGG_FIELDS= ['QUALapprox']
INFO_INT32_SUM_AGG_FIELDS = ['VarDP']
INFO_MEDIAN_AGG_FIELDS = ['ReadPosRankSum', 'MQRankSum']
INFO_ARRAY_SUM_AGG_FIELDS = ['SB', 'RAW_MQandDP']

def compute_last_ref_block_end(mt: hl.MatrixTable) -> hl.Table:
    """
    This function takes a sparse MT and computes for each row the genomic position of the
    most upstream reference block overlapping that row.

    Note that since reference blocks do not extend beyond contig boundaries, only the position is kept.

    This function returns a Table with that annotation.  (`last_END_position`).

    :param mt: Input MatrixTable
    :return: Output Table with `last_END_position` annotation
    """
    mt = mt.select_entries('END')

    # Localize entries, so that they can be viewed as an array and scanned over using hl.scan.array_agg
    ht = mt._localize_entries('__entries', '__cols')

    # Compute the position by using hl.scan._prev_nonnull.
    # This was inspired by hl.experimental.densify
    # _prev_non_null is an aggregator that keeps the previous record in memory
    # and updates it with the given value at the row if it's not null (missing)
    # The following code computes the following annotation for each row:
    # 1. Keep a scan of the entries using _prev_nonnull, keeping the start (ht.locus) and end (entry.END) of each ref block  (1.1)
    # 2. For the current row locus, record the start of the block that starts the furthest away,
    #    that is the minimum position in the current scan for any block that overlaps the current locus (2.1)
    ht = ht.select(
        last_END_position=hl.or_else(
            hl.min(  # 2. For the current row locus, record the start of the block that starts the furthest away
                hl.scan.array_agg(
                    lambda entry: hl.scan._prev_nonnull(  # 1. Keep a scan of the entries using _prev_nonnull
                        hl.or_missing(
                            hl.is_defined(entry.END),  # Update the scan whenever a new ref block is encountered
                            hl.tuple([  # 1.1 keep the start (ht.locus) and end (entry.END) of each ref block
                                ht.locus,
                                entry.END
                            ])
                        )
                    ),
                    ht.__entries
                ).map(
                    lambda x: hl.or_missing(  # 2.1 get the start position of blocks that overlap the current locus
                        (x[1] >= ht.locus.position) & (x[0].contig == ht.locus.contig),
                        x[0].position
                    )
                )
            ),
            ht.locus.position
        )
    )
    return ht.select_globals()


def densify_sites(
        mt: hl.MatrixTable,
        sites_ht: hl.Table,
        last_END_positions_ht: hl.Table,
        semi_join_rows: bool = True
) -> hl.MatrixTable:
    """
    Creates a dense version of the input sparse MT at the sites in `sites_ht` reading the minimal amount of data required.

    Note that only rows that appear both in `mt` and `sites_ht` are returned.

    :param mt: Input sparse MT
    :param sites_ht: Desired sites to densify
    :param last_END_positions_ht: Table storing positions of the furthest ref block (END tag)
    :param semi_join_rows: Whether to filter the MT rows based on semi-join (default, better if sites_ht is large) or based on filter_intervals (better if sites_ht only contains a few sites)
    :return: Dense MT filtered to the sites in `sites_ht`
    """
    logger.info("Computing intervals to densify from sites Table.")
    sites_ht = sites_ht.key_by('locus')
    sites_ht = sites_ht.annotate(
        interval=hl.locus_interval(
            sites_ht.locus.contig,
            last_END_positions_ht[sites_ht.key].last_END_position,
            end=sites_ht.locus.position,
            includes_end=True,
            reference_genome=sites_ht.locus.dtype.reference_genome
        )
    )
    sites_ht = sites_ht.filter(hl.is_defined(sites_ht.interval))

    if semi_join_rows:
        mt = mt.filter_rows(hl.is_defined(sites_ht.key_by('interval')[mt.locus]))
    else:
        logger.info("Collecting intervals to densify.")
        intervals = sites_ht.interval.collect()

        print("Found {0} intervals, totalling {1} bp in the dense Matrix.".format(
            len(intervals),
            sum([interval_length(interval) for interval in union_intervals(intervals)])
        ))

        mt = hl.filter_intervals(mt, intervals)

    mt = hl.experimental.densify(mt)

    return mt.filter_rows(
        hl.is_defined(sites_ht[mt.locus])
    )


def _get_info_agg_expr(
        mt: hl.MatrixTable,
        sum_agg_fields: Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_SUM_AGG_FIELDS,
        int32_sum_agg_fields: Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_INT32_SUM_AGG_FIELDS,
        median_agg_fields: Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_MEDIAN_AGG_FIELDS,
        array_sum_agg_fields: Union[List[str], Dict[str, hl.expr.ArrayNumericExpression]] = INFO_ARRAY_SUM_AGG_FIELDS,
        prefix: str = ''
) -> Dict[str, hl.expr.Aggregation]:
    """
    Helper function containing code to create Aggregators for both site or AS info expression aggregations.

    Notes:

    1. If `SB` is specified in array_sum_agg_fields, it will be aggregated as `AS_SB_TABLE`, according to GATK standard nomenclature.
    2. If `RAW_MQandDP` is specified in array_sum_agg_fields, it will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    3. If `RAW_MQ` and `MQ_DP` are given, they will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    4. If the fields to be aggregate (`sum_agg_fields`, `int32_sum_agg_fields`, `median_agg_fields`) are passed as list of str,
       then they should correspond to entry fields in `mt` or in `mt.gvcf_info`.
       Priority is given to entry fields in `mt` over those in `mt.gvcf_info` in case of a name clash.

    :param mt: Input MT
    :param sum_agg_fields: Fields to aggregate using sum.
    :param int32_sum_agg_fields: Fields to aggregate using sum using int32.
    :param median_agg_fields: Fields to aggregate using (approximate) median.
    :param median_agg_fields: Fields to aggregate using element-wise summing over an array.
    :param prefix: Optional prefix for the fields. Used for adding 'AS_' in the AS case.

    :return: Dictionary of expression names and their corresponding aggregation Expression
    """

    def _agg_list_to_dict(mt: hl.MatrixTable, fields: List[str]) -> Dict[str, hl.expr.NumericExpression]:
        out_fields = {}
        if 'gvcf_info' in mt.entry:
            out_fields = {f: mt.gvcf_info[f] for f in fields if f in mt.gvcf_info}

        out_fields.update(
            {f: mt[f] for f in fields if f in mt.entry}
        )

        #Check that all fields were found
        missing_fields = [f for f in fields if f not in out_fields]
        if missing_fields:
            raise  ValueError("Could not find the following field(s)in the MT entry schema (or nested under mt.gvcf_info: {}".format(
                ",".join(missing_fields)
            ))

        return out_fields

    # Map str to expressions where needed
    if isinstance(sum_agg_fields, list):
        sum_agg_fields = _agg_list_to_dict(mt, sum_agg_fields)

    if isinstance(int32_sum_agg_fields, list):
        int32_sum_agg_fields = _agg_list_to_dict(mt, int32_sum_agg_fields)

    if isinstance(median_agg_fields, list):
        median_agg_fields = _agg_list_to_dict(mt, median_agg_fields)

    if isinstance(array_sum_agg_fields, list):
        array_sum_agg_fields = _agg_list_to_dict(mt, array_sum_agg_fields)

    # Create aggregators
    agg_expr = {}

    agg_expr.update({
        f'{prefix}{k}': hl.agg.approx_quantiles(expr, 0.5)
        for k, expr in median_agg_fields.items()
    })
    agg_expr.update({
        f'{prefix}{k}': hl.agg.sum(expr)
        for k, expr in sum_agg_fields.items()
    })
    agg_expr.update({
        f'{prefix}{k}': hl.int32(hl.agg.sum(expr))
        for k, expr in int32_sum_agg_fields.items()
    })
    agg_expr.update({
        f'{prefix}{k}': hl.agg.array_agg(lambda x: hl.agg.sum(x), expr)
        for k, expr in array_sum_agg_fields.items()
    })

    # Handle annotations combinations and casting for specific annotations

    # If RAW_MQandDP is in agg_expr or if both MQ_DP and RAW_MQ are, compute MQ instead
    mq_tuple = None
    if f'{prefix}RAW_MQandDP' in agg_expr:
        logger.info(
            f"Computing {prefix}MQ as sqrt({prefix}RAW_MQandDP[0]/{prefix}RAW_MQandDP[1]). "
            f"Note that {prefix}MQ will be set to 0 if {prefix}RAW_MQandDP[1] == 0."
        )
        mq_tuple = agg_expr.pop(f'{prefix}RAW_MQandDP')
    elif f'{prefix}RAW_MQ' in agg_expr and f'{prefix}MQ_DP' in agg_expr:
        logger.info(
            f"Computing {prefix}MQ as sqrt({prefix}MQ_DP/{prefix}RAW_MQ). "
            f"Note that MQ will be set to 0 if {prefix}RAW_MQ == 0."
        )
        mq_tuple = (agg_expr.pop(f'{prefix}MQ_DP'), agg_expr.pop(f'{prefix}RAW_MQ'))

    if mq_tuple is not None:
        agg_expr[f'{prefix}MQ'] = hl.cond(
            mq_tuple[1] > 0,
            hl.sqrt(mq_tuple[0] / mq_tuple[1]),
            0
        )

    # If both VarDP and QUALapprox are present, also compute QD.
    if f"{prefix}VarDP" in agg_expr and f"{prefix}QUALapprox" in agg_expr:
        logger.info(
            f"Computing {prefix}QD as {prefix}QUALapprox/{prefix}VarDP. "
            f"Note that {prefix}QD will be set to 0 if {prefix}VarDP == 0."
        )
        var_dp = hl.int32(hl.agg.sum(int32_sum_agg_fields['VarDP']))
        agg_expr[f'{prefix}QD'] = hl.cond(var_dp > 0, agg_expr[f"{prefix}QUALapprox"] / var_dp, 0)

    # SB needs to be cast to int32 for FS down the line
    if f'{prefix}SB' in agg_expr:
        agg_expr[f'{prefix}SB'] = agg_expr[f'{prefix}SB'].map(lambda x: hl.int32(x))

    return agg_expr


def get_as_info_expr(
        mt: hl.MatrixTable,
        sum_agg_fields: Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_SUM_AGG_FIELDS,
        int32_sum_agg_fields:  Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_INT32_SUM_AGG_FIELDS,
        median_agg_fields:  Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_MEDIAN_AGG_FIELDS,
        array_sum_agg_fields: Union[List[str], Dict[str, hl.expr.ArrayNumericExpression]] = INFO_ARRAY_SUM_AGG_FIELDS
) -> hl.expr.StructExpression:
    """
    Returns an allele-specific annotation Struct containing typical VCF INFO fields from GVCF INFO fields stored in the MT entries.

    Notes:

    1. If `SB` is specified in array_sum_agg_fields, it will be aggregated as `AS_SB_TABLE`, according to GATK standard nomenclature.
    2. If `RAW_MQandDP` is specified in array_sum_agg_fields, it will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    3. If `RAW_MQ` and `MQ_DP` are given, they will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    4. If the fields to be aggregate (`sum_agg_fields`, `int32_sum_agg_fields`, `median_agg_fields`) are passed as list of str,
       then they should correspond to entry fields in `mt` or in `mt.gvcf_info`.
       Priority is given to entry fields in `mt` over those in `mt.gvcf_info` in case of a name clash.

    :param mt: Input Matrix Table
    :param sum_agg_fields: Fields to aggregate using sum.
    :param int32_sum_agg_fields: Fields to aggregate using sum using int32.
    :param median_agg_fields: Fields to aggregate using (approximate) median.
    :return: Expression containing the AS info fields
    """
    if 'DP' in list(sum_agg_fields) + list(int32_sum_agg_fields):
        logger.warning(
            "`DP` was included in allele-specific aggregation, "
            "however `DP` is typically not aggregated by allele; `VarDP` is."
            "Note that the resulting `AS_DP` field will NOT include reference genotypes."
        )

    agg_expr = _get_info_agg_expr(
        mt=mt,
        sum_agg_fields=sum_agg_fields,
        int32_sum_agg_fields=int32_sum_agg_fields,
        median_agg_fields=median_agg_fields,
        array_sum_agg_fields=array_sum_agg_fields,
        prefix='AS_'
    )

    # Rename AS_SB to AS_SB_TABLE if present
    if 'AS_SB' in agg_expr:
        agg_expr['AS_SB_TABLE'] = agg_expr.pop('AS_SB')

    # Modify aggregations to aggregate per allele
    agg_expr = {
        f: hl.agg.array_agg(
            lambda ai: hl.agg.filter(
                mt.LA.contains(ai),
                expr
            ),
            hl.range(1, hl.len(mt.alleles))
        )
        for f, expr in agg_expr.items()
    }

    # Run aggregations
    info = hl.struct(
        **agg_expr
    )

    # Add SB Ax2 aggregation logic and FS if SB is present
    if 'AS_SB_TABLE' in info:
        as_sb_table = hl.array([
            info.AS_SB_TABLE.filter(lambda x: hl.is_defined(x)).fold(lambda i, j: i[:2] + j[:2], [0, 0])  # ref
        ]).extend(
            info.AS_SB_TABLE.map(lambda x: x[2:])  # each alt
        )
        info = info.annotate(
            AS_SB_TABLE=as_sb_table,
            AS_FS=hl.range(1, hl.len(mt.alleles)).map(
                lambda i: fs_from_sb(as_sb_table[0].extend(as_sb_table[i]))
            )
        )

    return info


def get_site_info_expr(
        mt: hl.MatrixTable,
        sum_agg_fields: Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_SUM_AGG_FIELDS,
        int32_sum_agg_fields:  Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_INT32_SUM_AGG_FIELDS,
        median_agg_fields:  Union[List[str], Dict[str, hl.expr.NumericExpression]] = INFO_MEDIAN_AGG_FIELDS,
        array_sum_agg_fields: Union[List[str], Dict[str, hl.expr.ArrayNumericExpression]] = INFO_ARRAY_SUM_AGG_FIELDS
) -> hl.expr.StructExpression:
    """
    Creates a site-level annotation Struct aggregating typical VCF INFO fields from GVCF INFO fields stored in the MT entries.

    Notes:

    1. If `RAW_MQandDP` is specified in array_sum_agg_fields, it will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    2. If `RAW_MQ` and `MQ_DP` are given, they will be used for the `MQ` calculation and then dropped according to GATK recommendation.
    3. If the fields to be aggregate (`sum_agg_fields`, `int32_sum_agg_fields`, `median_agg_fields`) are passed as list of str,
       then they should correspond to entry fields in `mt` or in `mt.gvcf_info`.
       Priority is given to entry fields in `mt` over those in `mt.gvcf_info` in case of a name clash.

    :param mt: Input Matrix Table
    :param sum_agg_fields: Fields to aggregate using sum.
    :param int32_sum_agg_fields: Fields to aggregate using sum using int32.
    :param median_agg_fields: Fields to aggregate using (approximate) median.
    :return: Expression containing the site-level info fields
    """
    if 'DP' in list(sum_agg_fields) + list(int32_sum_agg_fields):
        logger.warning("`DP` was included in site-level aggregation. This requires a densifying prior to running get_site_info_expr")

    agg_expr = _get_info_agg_expr(
        mt=mt,
        sum_agg_fields=sum_agg_fields,
        int32_sum_agg_fields=int32_sum_agg_fields,
        median_agg_fields=median_agg_fields,
        array_sum_agg_fields=array_sum_agg_fields
    )

    # Add FS if SB is present
    # This is done outside of _get_info_agg_expr as the behavior is different in site vs allele-specific versions
    agg_expr['FS'] = fs_from_sb(agg_expr['SB'])

    # Run aggregator on non-ref genotypes
    info = hl.agg.filter(
        mt.LGT.is_non_ref(),
        hl.struct(
            **{k: v for k, v in agg_expr.items() if k != 'DP'}
        )
    )

    # Add DP, computed over both ref and non-ref genotypes, if present
    if 'DP' in agg_expr:
        info = info.annotate(
            DP=agg_expr['DP']
        )

    return info


def impute_sex_ploidy(
        mt: hl.MatrixTable,
        excluded_intervals: Optional[hl.Table] = None,
        included_intervals: Optional[hl.Table] = None,
        normalization_contig: str = 'chr20',
        chr_x: Optional[str] = None,
        chr_y: Optional[str] = None,
) -> hl.Table: # TODO: For exomes, calling intervals need to be added
    """
    Imputes sex ploidy from a sparse Matrix Table by normalizing the coverage of chromosomes X and Y using
    the coverage of an autosomal chromosome (by default chr20).
    Coverage is computed using the median block coverage (summed over the block size) and the non-ref coverage at non-ref genotypes.

    :param mt: Input sparse Matrix Table
    :param excluded_intervals: Optional table of intervals to exclude from the computation.
    :param included_intervals: Optional table of intervals to use in the computation. REQUIRED for exomes.
    :param normalization_contig: Which chromosome to normalize by
    :param chr_x: Optional X Chromosome contig name (by default uses the X contig in the reference)
    :param chr_y: Optional Y Chromosome contig name (by default uses the Y contig in the reference)
    :return: Table with mean coverage over chromosomes 20, X and Y and sex chromosomes ploidy based on normalized coverage.
    """

    ref = get_reference_genome(mt.locus, add_sequence=True)
    if chr_x is None:
        if len(ref.x_contigs) != 1:
            raise NotImplementedError(
                "Found {0} X chromosome contigs ({1}) in Genome reference. sparse_impute_sex_ploidy currently only supports a single X chromosome contig. Please use the `chr_x` argument to  specify which X chromosome contig to use ".format(
                    len(ref.x_contigs),
                    ",".join(ref.x_contigs)
                )
            )
        chr_x = ref.x_contigs[0]
    if chr_y is None:
        if len(ref.y_contigs) != 1:
            raise NotImplementedError(
                "Found {0} Y chromosome contigs ({1}) in Genome reference. sparse_impute_sex_ploidy currently only supports a single Y chromosome contig. Please use the `chr_y` argument to  specify which Y chromosome contig to use ".format(
                    len(ref.y_contigs),
                    ",".join(ref.y_contigs)
                )
            )
        chr_y = ref.y_contigs[0]

    def get_contig_size(contig: str) -> int:
        logger.info(f"Working on {contig}")
        contig_ht = hl.utils.range_table(ref.contig_length(contig), n_partitions=int(ref.contig_length(contig) / 500_000))
        contig_ht = contig_ht.annotate(
            locus=hl.locus(contig=contig, pos=contig_ht.idx + 1, reference_genome=ref)
        )
        contig_ht = contig_ht.filter(contig_ht.locus.sequence_context().lower() != 'n')

        if contig in ref.x_contigs:
            contig_ht = contig_ht.filter(contig_ht.locus.in_x_nonpar())
        if contig in ref.y_contigs:
            contig_ht = contig_ht.filter(contig_ht.locus.in_y_nonpar())

        contig_ht = contig_ht.key_by('locus')
        if included_intervals is not None:
            contig_ht = contig_ht.filter(hl.is_defined(included_intervals[contig_ht.key]))
        if excluded_intervals is not None:
            contig_ht = contig_ht.filter(hl.is_missing(excluded_intervals[contig_ht.key]))
        contig_size = contig_ht.count()
        logger.info(f"Contig {contig} has {contig_size} bases for coverage.")
        return contig_size

    def get_chr_dp_ann(chrom: str) -> hl.Table:
        contig_size = get_contig_size(chrom)
        chr_mt = hl.filter_intervals(mt, [hl.parse_locus_interval(chrom)])

        if chrom in ref.x_contigs:
            chr_mt = chr_mt.filter_rows(chr_mt.locus.in_x_nonpar())
        if chrom in ref.y_contigs:
            chr_mt = chr_mt.filter_rows(chr_mt.locus.in_y_nonpar())

        if included_intervals is not None:
            chr_mt = chr_mt.filter_rows(hl.is_defined(included_intervals[chr_mt.locus]))

        return chr_mt.select_cols(**{
            f'{chrom}_mean_dp': hl.agg.sum(hl.cond(chr_mt.LGT.is_hom_ref(), chr_mt.DP * (chr_mt.END - chr_mt.locus.position), chr_mt.DP)) / contig_size
        }).cols()

    normalization_chrom_dp = get_chr_dp_ann(normalization_contig)
    chrX_dp = get_chr_dp_ann(chr_x)
    chrY_dp = get_chr_dp_ann(chr_y)

    ht = normalization_chrom_dp.annotate(
        **chrX_dp[normalization_chrom_dp.key],
        **chrY_dp[normalization_chrom_dp.key],
    )

    return ht.annotate(
        **{
            f'{chr_x}_ploidy': ht[f'{chr_x}_mean_dp'] / (ht[f'{normalization_contig}_mean_dp'] / 2),
            f'{chr_y}_ploidy': ht[f'{chr_y}_mean_dp'] / (ht[f'{normalization_contig}_mean_dp'] / 2)
        }
    )


def compute_coverage_stats(
        mt: hl.MatrixTable,
        reference_ht: hl.Table,
        coverage_over_x_bins: List[int] = [1, 5, 10, 15, 20, 25, 30, 50, 100]
) -> hl.Table:
    """
    Computes the following coverage statistics for every base of the `reference_ht` provided:
        - mean
        - median
        - total DP
        - fraction of samples with coverage above X, for each x in `coverage_over_x_bins`

    The `reference_ht` can e.g. be created using `get_reference_ht`

    :param mt: Input sparse MT
    :param reference_ht: Input reference HT
    :param coverage_over_x_bins: List of boundaries for computing samples over X
    :return: Table with per-base coverage stats
    """

    n_samples = mt.count_cols()
    print(f"Computing coverage stats on {n_samples} samples.")

    # Create an outer join with the reference Table
    mt = mt.select_entries('END', 'DP')
    col_key_fields = list(mt.col_key)
    t = mt._localize_entries('__entries', '__cols')
    t = t.join(reference_ht.annotate(_in_ref=True), how='outer')
    t = t.annotate(__entries=hl.or_else(t.__entries, hl.range(n_samples).map(lambda x: hl.null(t.__entries.dtype.element_type))))
    mt = t._unlocalize_entries('__entries', '__cols', col_key_fields)

    # Densify
    mt = hl.experimental.densify(mt)

    # Filter rows where the reference is missing
    mt = mt.filter_rows(mt._in_ref)

    # Compute coverage stats
    coverage_over_x_bins = sorted(coverage_over_x_bins)
    max_coverage_bin = coverage_over_x_bins[-1]
    hl_coverage_over_x_bins = hl.array(coverage_over_x_bins)

    # This expression creates a counter DP -> number of samples for DP between 0 and max_coverage_bin
    coverage_counter_expr = hl.agg.counter(hl.or_else(hl.min(max_coverage_bin, mt.DP), 0))

    # This expression aggregates the DP counter in reverse order of the coverage_over_x_bins
    # and computes the cumulative sum over them.
    #  It needs to be in reverse order because we want the sum over samples covered by > X.
    count_array_expr = hl.cumulative_sum(
        hl.array(
            [hl.int32(coverage_counter_expr.get(max_coverage_bin, 0))]  # The coverage was already floored to the max_coverage_bin, so no more aggregation is needed for the max bin
        ).extend(  # For each of the other bins, coverage needs to be summed between the boundaries
            hl.range(hl.len(hl_coverage_over_x_bins) - 1, 0, step=-1).map(
                lambda i: hl.sum(
                    hl.range(hl_coverage_over_x_bins[i - 1], hl_coverage_over_x_bins[i]).map(
                        lambda j: hl.int32(coverage_counter_expr.get(j, 0))
                    )
                )
            )
        )
    )
    mean_expr = hl.agg.mean(mt.DP)

    # Annotate rows now
    return mt.select_rows(
        mean=hl.cond(hl.is_nan(mean_expr), 0, mean_expr),
        median=hl.or_else(hl.agg.approx_median(mt.DP), 0),
        total_DP=hl.agg.sum(mt.DP),
        **{
            f'over_{x}': count_array_expr[i] / n_samples
            for i, x in zip(
                range(len(coverage_over_x_bins) - 1, -1, -1),  # Reverse the bin index as count_array_expr has the reverse order
                coverage_over_x_bins
            )
        }
    ).rows()
