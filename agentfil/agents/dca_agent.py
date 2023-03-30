from datetime import timedelta
from .. import constants
from .sp_agent import SPAgent
from ..power import cc_power, deal_power
from ..filecoin_model import apply_qa_multiplier

import numpy as np
import pandas as pd

class DCAAgent(SPAgent):
    """
    The Dollar-Cost-Averaging agent is a simple agent that onboards a fixed amount of power every day
    This is based on the common investment strategy of dollar-cost-averaging.

    TODO
     [ ] - vectorize the onboarding, renewal rate and FIL+ rates
    """
    def __init__(self, model, id, historical_power, start_date, end_date,
                 max_daily_rb_onboard_pib=3, renewal_rate=0.6, fil_plus_rate=0.6, sector_duration=360):
        super().__init__(model, id, historical_power, start_date, end_date)

        self.sector_duration = sector_duration
        self.sector_duration_yrs = sector_duration / 360
        self.max_daily_rb_onboard_pib = max_daily_rb_onboard_pib
        self.renewal_rate = renewal_rate
        self.fil_plus_rate = fil_plus_rate

    def step(self):
        rb_to_onboard = min(self.max_daily_rb_onboard_pib, self.max_sealing_throughput_pib)
        qa_to_onboard = apply_qa_multiplier(rb_to_onboard * self.fil_plus_rate)
        pledge_per_pib = self.model.estimate_pledge_for_qa_power(self.current_date, 1.0)
        
        # debugging to ensure that pledge/sector seems reasonable
        # sector_size_in_pib = constants.SECTOR_SIZE / constants.PIB
        # pledge_per_sector = self.model.estimate_pledge_for_qa_power(self.current_date, sector_size_in_pib)
        # print(pledge_per_pib, pledge_per_sector)
        
        total_qa_onboarded = rb_to_onboard + qa_to_onboard
        pledge_needed_for_onboarding = total_qa_onboarded * pledge_per_pib
        pledge_repayment_value_onboard = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                       pledge_needed_for_onboarding, 
                                                                                                       self.sector_duration_yrs)

        
        self.onboard_power(self.current_date, rb_to_onboard, total_qa_onboarded, self.sector_duration, 
                           pledge_needed_for_onboarding, pledge_repayment_value_onboard)

        # renewals
        if self.renewal_rate > 0:
            se_power_dict = self.get_se_power_at_date(self.current_date)
            # only renew CC power
            cc_power = se_power_dict['se_cc_power']
            cc_power_to_renew = cc_power*self.renewal_rate  # we don't cap renewals, TODO: check whether this is a reasonable assumption

            pledge_needed_for_renewal = cc_power_to_renew * pledge_per_pib
            pledge_repayment_value_renew = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                         pledge_needed_for_renewal, 
                                                                                                         self.sector_duration_yrs)

            self.renew_power(self.current_date, cc_power_to_renew, self.sector_duration,
                             pledge_needed_for_renewal, pledge_repayment_value_renew)

        super().step()
