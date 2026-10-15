/*
 * BMS - battery management system, the device under test.
 *
 * Phase 1 scope: boot, expose the injection targets, implement ONE safety
 * function, and publish a status frame the harness can assert against.
 *
 * The single safety function here is over-temperature protection. Phase 2 adds
 * the other seven. It exists now because Phase 1 needs a real rule to build the
 * canary defect against: inverting its comparison from > to >= is the whole
 * basis of the engine's red/green test.
 *
 * Determinism is load bearing. Every timing result in the paper rests on two
 * runs producing byte-identical console output, so this file contains,
 * deliberately:
 *
 *   no wall-clock reads   nothing calls k_uptime_get(), and nothing prints a
 *                         time. The tick counter is a pure count of loop
 *                         iterations, not a measurement of elapsed time.
 *   no floating point     every printed and transmitted value is an integer.
 *   no randomness         no entropy source, no hashed pointer, no address.
 */

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <stdint.h>

/* Status frame. The signal layout is declared in systems/powertrain/catalog.yml
 * and must match it byte for byte; the catalogue is the contract, this is one
 * implementation of it. */
#define BMS_STATUS_ID   0x604U

/* The peer's heartbeat. The BMS counts these so that peer-loss supervision has
 * something to supervise; a later requirement family depends on it. */
#define VCU_STATUS_ID   0x605U

/* Fault codes. Mirrors the fault_code enum in the catalogue. */
#define FAULT_NONE      0U
#define FAULT_OVERTEMP  1U

/*
 * CANARY BUILD -- DEFECTIVE ON PURPOSE.
 *
 * The comparison below has lost its strictness: >= where the golden build
 * has >. A reading exactly at the limit, which the requirement says is
 * legal, now raises a fault. This is a test fixture for the engine, not a
 * member of the defect corpus.
 *
 * Over-temperature limit, in tenths of a degree Celsius.
 *
 * The comparison below is STRICT. A reading exactly at the limit is legal and
 * must not raise a fault; only a reading above it may. That single character is
 * what the canary defect inverts, and the difference is observable at exactly
 * one input value.
 */
#define OVERTEMP_LIMIT_dC   550

/* Fixed tick. A loop period expressed as a constant, never as a measured
 * interval, so the schedule is identical on every run. */
#define TICK_MS         100U
#define REPORT_EVERY    10U      /* one console report per second */

/* ---------------------------------------------------------------------------
 * Injection targets.
 *
 * The harness resolves each of these through the ELF symbol table and writes the
 * running machine's memory directly. At instruction level the firmware cannot
 * distinguish that from a sensor driver's write.
 *
 * g_pack_mv is uint32_t, not uint16_t. In millivolts a 16-bit field caps at
 * 65.535 V, and this project's powertrain pack is a high-voltage one: 400 V is
 * 400000 mV, which wraps to 7616 in 16 bits. That wrap would look exactly like a
 * planted defect, and it would be in the GOLDEN firmware, contaminating every
 * voltage result in the paper. The unit stays millivolts; the type is widened.
 *
 * `volatile` forces a real load on every loop iteration so the compiler cannot
 * cache a value in a register across the sleep. It does NOT protect the symbol
 * from the linker; see injectable_symbols.txt and CMakeLists.txt.
 * ------------------------------------------------------------------------- */
volatile int16_t  g_cell_temp_dC = 250;    /* tenths of a degree C, 25.0 C     */
volatile uint32_t g_pack_mv      = 48000;  /* millivolts                       */
volatile int16_t  g_pack_ma      = -5000;  /* milliamps, signed: negative is
                                              discharge                        */
volatile uint8_t  g_tx_enable    = 1;      /* transmit enable                  */

/* ---------------------------------------------------------------------------
 * Garbage-collection canary.
 *
 * Referenced by NOTHING. No line of C reads or writes it. It survives the link
 * only because of its -Wl,--undefined flag, which makes it the negative control
 * for the symbol gate: withholding that one flag lets the linker discard it and
 * the gate must then refuse the binary. It is not an injection target and is
 * marked not-injectable in injectable_symbols.txt.
 * ------------------------------------------------------------------------- */
volatile uint32_t g_gc_canary = 0xC0DEC0DEU;

static const struct device *const can_dev =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));

/* Written from the receive callback, read from the main loop. */
static volatile uint32_t s_rx_count;

static void vcu_rx_cb(const struct device *dev, struct can_frame *frame,
		      void *user_data)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(frame);
	ARG_UNUSED(user_data);
	s_rx_count++;
}

/* Latched fault state. Once raised it is never cleared here; clearing on reset
 * only is the behaviour the requirement describes, and a later defect family
 * attacks exactly that. */
static uint8_t s_fault_code = FAULT_NONE;

int main(void)
{
	uint32_t tick = 0U;
	uint8_t  rolling = 0U;
	int can_rc;

	/* Printed before anything that can fail, so a CAN problem is diagnosed as
	 * a CAN problem and never as a dead console. */
	printk("BMS ready\n");

	if (!device_is_ready(can_dev)) {
		printk("bms FATAL can device not ready\n");
		return -1;
	}

	{
		/*
		 * CAN_FILTER_DATA is REQUIRED.
		 *
		 * At Zephyr 3.5 a filter with flags = 0 matches nothing at all.
		 * The node boots, announces itself, and receives not one frame,
		 * which is indistinguishable from a peer that never transmitted.
		 * If the receive count stays at zero, check this flag before
		 * suspecting the peer, the hub, or the bit rate.
		 */
		const struct can_filter filter = {
			.id    = VCU_STATUS_ID,
			.mask  = CAN_STD_ID_MASK,
			.flags = CAN_FILTER_DATA,
		};
		int filter_id = can_add_rx_filter(can_dev, vcu_rx_cb, NULL, &filter);

		printk("bms add_rx_filter rc=%d\n", filter_id);
	}

	can_rc = can_start(can_dev);
	printk("bms can_start rc=%d\n", can_rc);

	while (1) {
		/* Read every injection target on every iteration, so an injected
		 * write becomes observable on the next tick. */
		int16_t  temp_dC = g_cell_temp_dC;
		uint32_t pack_mv = g_pack_mv;
		int16_t  pack_ma = g_pack_ma;
		uint8_t  tx_on   = g_tx_enable;

		/* --- SAFETY FUNCTION: over-temperature protection ---------------
		 * Strict comparison. At exactly the limit the reading is legal.
		 * Latched: once set, it stays set. */
		if (temp_dC >= OVERTEMP_LIMIT_dC) {
			s_fault_code = FAULT_OVERTEMP;
		}

		{
			struct can_frame frame = {
				.id    = BMS_STATUS_ID,
				.dlc   = 8U,
				.flags = 0U,
			};
			uint8_t sum = 0U;
			int i;
			int tx_rc = 0;

			/* Little-endian bit packing, matching catalog.yml:
			 *   fault_code       start_bit  0, length  8  -> byte 0
			 *   pack_mv          start_bit  8, length 24  -> bytes 1..3
			 *   pack_ma          start_bit 32, length 16  -> bytes 4..5, signed
			 *   rolling_counter  start_bit 48, length  4  -> byte 6, low nibble
			 *   checksum         start_bit 56, length  8  -> byte 7
			 */
			frame.data[0] = s_fault_code;
			frame.data[1] = (uint8_t)(pack_mv & 0xFFU);
			frame.data[2] = (uint8_t)((pack_mv >> 8) & 0xFFU);
			frame.data[3] = (uint8_t)((pack_mv >> 16) & 0xFFU);
			frame.data[4] = (uint8_t)((uint16_t)pack_ma & 0xFFU);
			frame.data[5] = (uint8_t)(((uint16_t)pack_ma >> 8) & 0xFFU);
			frame.data[6] = (uint8_t)(rolling & 0x0FU);

			/* Checksum over the preceding seven bytes. A later defect
			 * family changes this range. */
			for (i = 0; i < 7; i++) {
				sum = (uint8_t)(sum + frame.data[i]);
			}
			frame.data[7] = sum;

			if (tx_on) {
				tx_rc = can_send(can_dev, &frame, K_MSEC(50),
						 NULL, NULL);
			}

			/* The rolling counter advances on every transmission
			 * attempt, which is exactly why assertions must match on
			 * (value, mask) and never on equality. */
			rolling = (uint8_t)((rolling + 1U) & 0x0FU);

			if ((tick % REPORT_EVERY) == 0U) {
				printk("bms tick=%u temp_dC=%d pack_mv=%u pack_ma=%d "
				       "fault=%u tx=%u txrc=%d rx_from_vcu=%u\n",
				       tick, (int)temp_dC, (unsigned int)pack_mv,
				       (int)pack_ma, (unsigned int)s_fault_code,
				       (unsigned int)tx_on, tx_rc,
				       (unsigned int)s_rx_count);
			}
		}

		tick++;
		k_sleep(K_MSEC(TICK_MS));
	}

	return 0;
}
