module prim_arbiter_ppc (
	clk_i,
	rst_ni,
	req_i,
	data_i,
	gnt_o,
	idx_o,
	valid_o,
	data_o,
	ready_i
);
	parameter [31:0] N = 4;
	parameter [31:0] DW = 32;
	input clk_i;
	input rst_ni;
	input [N - 1:0] req_i;
	input [(0 >= (N - 1) ? ((DW - 1) >= 0 ? ((2 - N) * DW) + (((N - 1) * DW) - 1) : ((2 - N) * (2 - DW)) + (((DW - 1) + ((N - 1) * (2 - DW))) - 1)) : ((DW - 1) >= 0 ? (N * DW) + -1 : (N * (2 - DW)) + ((DW - 1) - 1))):(0 >= (N - 1) ? ((DW - 1) >= 0 ? (N - 1) * DW : (DW - 1) + ((N - 1) * (2 - DW))) : ((DW - 1) >= 0 ? 0 : DW - 1))] data_i;
	output wire [N - 1:0] gnt_o;
	output reg [$clog2(N) - 1:0] idx_o;
	output wire valid_o;
	output reg [DW - 1:0] data_o;
	input ready_i;
	generate
		if (N == 1) begin : gen_degenerate_case
			assign valid_o = req_i[0];
			always @(*) data_o = data_i[((DW - 1) >= 0 ? 0 : DW - 1) + ((0 >= (N - 1) ? 0 : N - 1) * ((DW - 1) >= 0 ? DW : 2 - DW))+:((DW - 1) >= 0 ? DW : 2 - DW)];
			assign gnt_o[0] = valid_o & ready_i;
			always @(*) idx_o = 1'sb0;
		end
		else begin : gen_normal_case
			wire [N - 1:0] masked_req;
			reg [N - 1:0] ppc_out;
			wire [N - 1:0] arb_req;
			reg [N - 1:0] mask;
			wire [N - 1:0] mask_next;
			wire [N - 1:0] winner;
			assign masked_req = mask & req_i;
			assign arb_req = (|masked_req ? masked_req : req_i);
			always @(*) begin
				ppc_out[0] = arb_req[0];
				begin : sv2v_autoblock_136
					reg signed [31:0] i;
					for (i = 1; i < N; i = i + 1)
						ppc_out[i] = ppc_out[i - 1] | arb_req[i];
				end
			end
			assign winner = ppc_out ^ {ppc_out[N - 2:0], 1'b0};
			assign gnt_o = (ready_i ? winner : 1'sb0);
			assign valid_o = |req_i;
			assign mask_next = {ppc_out[N - 2:0], 1'b0};
			always @(posedge clk_i or negedge rst_ni)
				if (!rst_ni)
					mask <= 1'sb0;
				else if (valid_o && ready_i)
					mask <= mask_next;
				else if (valid_o && !ready_i)
					mask <= ppc_out;
			always @(*) begin
				data_o = 1'sb0;
				idx_o = 1'sb0;
				begin : sv2v_autoblock_137
					reg signed [31:0] i;
					for (i = 0; i < N; i = i + 1)
						if (winner[i]) begin
							data_o = data_i[((DW - 1) >= 0 ? 0 : DW - 1) + ((0 >= (N - 1) ? i : (N - 1) - i) * ((DW - 1) >= 0 ? DW : 2 - DW))+:((DW - 1) >= 0 ? DW : 2 - DW)];
							idx_o = i;
						end
				end
			end
		end
	endgenerate
endmodule