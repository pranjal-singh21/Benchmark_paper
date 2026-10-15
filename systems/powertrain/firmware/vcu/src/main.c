/*
 * VCU - vehicle control unit, the BMS's peer on the bus.
 *
 * Phase 1 scope: be a second real node. The engine cannot be tested with one
 * machine, and two of its guarantees are untestable without a peer:
 *
 *   - whole-bus matching. An assertion must see frames sent by nodes other than
 *     the device under test. Without a peer that transmits an identifier the DUT
 *     never sends, a missing bus tap cannot be detected, and a missing tap makes
 *     expect_no_can report clean while matching traffic flows.
 *   - inter-node reception. A heartbeat is only meaningful if something is
 *     listening for it.
 *
 * Determinism rules are the same as the BMS: no wall-clock reads, no floating
 * point, no randomness.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <stdint.h>

/* This node's heartbeat. Deliberately an identifier the BMS never sends, so a
 * scenario can arm an absence assertion on it and prove the harness taps every
 * real node rather than only the device under test. */
#define VCU_STATUS_ID   0x605U

/* The identifier this node listens for. */
#define BMS_STATUS_ID   0x604U

#define TICK_MS         100U
#define REPORT_EVERY    10U

/* Injection targets. g_tx_enable is required of every real node by network.yml,
 * which names it as the node's tx_enable_symbol. */
volatile uint8_t  g_tx_enable    = 1;
volatile uint16_t g_peer_timeout_ms = 300;

/* Referenced by nothing; the linker-collection negative control for this node.
 * See the BMS source for the full explanation. */
volatile uint32_t g_gc_canary = 0xCAFEC0DEU;

static const struct device *const can_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));

/* Written from the receive callback, read from the main loop. */
static volatile uint32_t s_rx_count;

static void bms_rx_cb(const struct device *dev, struct can_frame *frame,
		      void *user_data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(frame);
	ARG_UNUSED(user_data);
	s_rx_count++;
}

int main(void)
{
	uint32_t tick = 0U;
	int can_rc;
	int filter_id;

	printk("VCU ready\n");

	if (!device_is_ready(can_dev)) {
		printk("vcu FATAL can device not ready\n");
		return -1;
	}

	{
		/*
		 * CAN_FILTER_DATA is REQUIRED.
		 *
		 * In Zephyr 3.5 a filter with flags = 0 matches nothing at all.
		 * The node boots, announces itself, and receives not one frame,
		 * which is indistinguishable from a peer that never transmitted.
		 * If the receive count below is zero, check this flag before
		 * suspecting the peer, the hub, or the bit rate.
		 */
		const struct can_filter filter = {
			.id    = BMS_STATUS_ID,
			.mask  = CAN_STD_ID_MASK,
			.flags = CAN_FILTER_DATA,
		};

		filter_id = can_add_rx_filter(can_dev, bms_rx_cb, NULL, &filter);
		printk("vcu add_rx_filter rc=%d\n", filter_id);
	}

	can_rc = can_start(can_dev);
	printk("vcu can_start rc=%d\n", can_rc);

	while (1) {
		uint8_t tx_on = g_tx_enable;
		struct can_frame frame = {
			.id    = VCU_STATUS_ID,
			.dlc   = 8U,
			.flags = 0U,
		};
		int tx_rc = 0;
		uint32_t seen = s_rx_count;

		/* Heartbeat payload: the count of BMS frames observed, so the
		 * console and the bus agree about what this node has seen. */
		frame.data[0] = (uint8_t)(seen & 0xFFU);
		frame.data[1] = (uint8_t)((seen >> 8) & 0xFFU);
		frame.data[2] = 0U;
		frame.data[3] = 0U;
		frame.data[4] = 0U;
		frame.data[5] = 0U;
		frame.data[6] = 0U;
		frame.data[7] = 0U;

		if (tx_on) {
			tx_rc = can_send(can_dev, &frame, K_MSEC(50), NULL, NULL);
		}

		if ((tick % REPORT_EVERY) == 0U) {
			printk("vcu tick=%u rx_from_bms=%u tx=%u txrc=%d\n",
			       tick, (unsigned int)seen, (unsigned int)tx_on,
			       tx_rc);
		}

		tick++;
		k_sleep(K_MSEC(TICK_MS));
	}

	return 0;
}
