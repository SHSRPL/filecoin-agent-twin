from datetime import timedelta
from .. import constants
from .sp_agent import SPAgent
from ..power import cc_power, deal_power

import numpy as np
import pandas as pd

class ROIAgent(SPAgent):
    """
    The ROI agent is an agent that uses ROI forecasts to decide how much power to onboard.
    If the ROI exceeds the defined threshold, then the agent will decide to onboard the maximum available power, 
    otherwise they will not.

    TODO:
     [ ] - 
    """
    def __init__(self, model, id, historical_power, start_date, end_date,
                 max_sealing_throughput=constants.DEFAULT_MAX_SEALING_THROUGHPUT_PIB, max_daily_rb_onboard_pib=3,
                 renewal_rate = 0.6, fil_plus_rate=0.6, 
                 agent_optimism=4, roi_threshold=0.1, debug_mode=False):
        """

        debug_mode - if True, the agent will compute the power scheduled to be onboarded/renewed, but will not actually
                     onboard/renew that power, but rather return the values.  This can be used for debugging
                     or other purposes
        """
        super().__init__(model, id, historical_power, start_date, end_date, max_sealing_throughput_pib=max_sealing_throughput)

        self.max_daily_rb_onboard_pib = max_daily_rb_onboard_pib
        self.renewal_rate = renewal_rate
        self.fil_plus_rate = fil_plus_rate

        print(f"ROI agent {id} - max_daily_rb_onboard_pib: {self.max_daily_rb_onboard_pib}, renewal_rate: {self.renewal_rate}, fil_plus_rate: {self.fil_plus_rate}")

        self.roi_threshold = roi_threshold
        self.agent_optimism = agent_optimism

        self.duration_vec_days = np.asarray([360, 360*3]).astype(np.int32)  # 1Y, 3Y, 5Y sectors are possible

        self.map_optimism_scales()

        for d in self.duration_vec_days:
            self.agent_info_df[f'roi_estimate_{d}'] = 0

        self.debug_mode = debug_mode

    def map_optimism_scales(self):
        self.optimism_to_price_quantile_str = {
            1 : "Q05",
            2 : "Q25",
            3 : "Q50",
            4 : "Q75",
            5 : "Q95"
        }
        self.optimism_to_dayrewardspersector_quantile_str = {
            1 : "Q05",
            2 : "Q25",
            3 : "Q50",
            4 : "Q75",
            5 : "Q95"
        }

    def forecast_day_rewards_per_sector(self, forecast_start_date, forecast_length):
        k = 'day_rewards_per_sector_forecast_' + self.optimism_to_dayrewardspersector_quantile_str[self.agent_optimism]
        start_idx = self.model.global_forecast_df[pd.to_datetime(self.model.global_forecast_df['date']) == pd.to_datetime(forecast_start_date)].index[0]
        end_idx = start_idx + forecast_length
        future_rewards_per_sector = self.model.global_forecast_df.loc[start_idx:end_idx, k].values
        
        return future_rewards_per_sector


    def estimate_roi(self, sector_duration, date_in):
        filecoin_df_idx = self.model.filecoin_df[pd.to_datetime(self.model.filecoin_df['date']) == pd.to_datetime(date_in)].index[0]

        # NOTE: we need to use yesterday's metrics b/c today's haven't yet been aggregated by the system yet
        prev_day_pledge_per_QAP = self.model.filecoin_df.loc[filecoin_df_idx-1, 'day_pledge_per_QAP']

        # TODO: make this an iterative update rather than full-estimate every day
        # NOTE: this assumes that the pledge remains constant. This is not true, but a zeroth-order approximation
        future_rewards_per_sector_estimate = self.forecast_day_rewards_per_sector(date_in, sector_duration)
        
        # get the cost per sector for the duration, which in this case is just borrowing costs
        sector_duration_yrs = sector_duration / 360.
        pledge_repayment_estimate = self.compute_repayment_amount_from_supply_discount_rate_model(date_in, prev_day_pledge_per_QAP, sector_duration_yrs)
        cost_per_sector_estimate = pledge_repayment_estimate - prev_day_pledge_per_QAP
        total_rewards_per_sector_estimate = future_rewards_per_sector_estimate.sum()
        if prev_day_pledge_per_QAP > 0:
            roi_estimate = (total_rewards_per_sector_estimate - cost_per_sector_estimate) / prev_day_pledge_per_QAP
        else:
            roi_estimate = 1
        
        # annualize it so that we can have the same frame of reference when comparing different sector durations
        if roi_estimate < -1:
            roi_estimate_annualized = self.roi_threshold - 1  # if ROI is too low, set it so that it doesn't onboard.
                                                              # otherwise, you would take an exponent of a negative number
                                                              # to a fractional power below and get a complex number
        else:
            roi_estimate_annualized = (1.0+roi_estimate)**(1.0/sector_duration_yrs) - 1
        
        # print(roi_estimate, roi_estimate_annualized, duration_yr)
        # if np.isnan(future_rewards_per_sector_estimate.sum()) or np.isnan(prev_day_pledge_per_QAP) or np.isnan(roi_estimate) or np.isnan(roi_estimate_annualized):
        #     print(self.unique_id, future_rewards_per_sector_estimate.sum(), prev_day_pledge_per_QAP, roi_estimate, roi_estimate_annualized)

        return roi_estimate_annualized

    def step(self):
        roi_estimate_vec = []
        
        agent_df_idx = self.agent_info_df[pd.to_datetime(self.agent_info_df['date']) == pd.to_datetime(self.current_date)].index[0]
        for d in self.duration_vec_days:    
            roi_estimate = self.estimate_roi(d, self.current_date)            
            roi_estimate_vec.append(roi_estimate)
            self.agent_info_df.loc[agent_df_idx, 'roi_estimate_%d' % (d,)] = roi_estimate
            
        max_roi_idx = np.argmax(roi_estimate_vec)
        best_duration = self.duration_vec_days[max_roi_idx]
        best_duration_yrs = best_duration / 360.
        if roi_estimate_vec[max_roi_idx] > self.roi_threshold:
            rb_to_onboard = min(self.max_daily_rb_onboard_pib, self.max_sealing_throughput_pib)
            qa_to_onboard = self.model.apply_qa_multiplier(rb_to_onboard * self.fil_plus_rate,
                                                       fil_plus_multipler=constants.FIL_PLUS_MULTIPLER,
                                                       date_in=self.current_date,
                                                       sector_duration_days=best_duration) + \
                        rb_to_onboard * (1-self.fil_plus_rate)
            pledge_per_pib = self.model.estimate_pledge_for_qa_power(self.current_date, 1.0)

            pledge_needed_for_onboarding = qa_to_onboard * pledge_per_pib
            pledge_repayment_value_onboard = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                        pledge_needed_for_onboarding, 
                                                                                                        best_duration_yrs)

            if not self.debug_mode:
                self.onboard_power(self.current_date, rb_to_onboard, qa_to_onboard, best_duration,
                                   pledge_needed_for_onboarding, pledge_repayment_value_onboard)
            else:
                onboard_args_to_return = (self.current_date, rb_to_onboard, qa_to_onboard, best_duration, 
                                          pledge_needed_for_onboarding, pledge_repayment_value_onboard)

            if self.renewal_rate > 0:
                # renew available power for the same duration
                se_power_dict = self.get_se_power_at_date(self.current_date)
                # which aspects of power get renewed is dependent on the setting "renewals_setting" in the FilecoinModel object
                cc_power_to_renew = se_power_dict['se_cc_power'] * self.renewal_rate
                deal_power_to_renew = se_power_dict['se_deal_power'] * self.renewal_rate

                # print('ROI[%d]:' % (self.unique_id,), se_power_dict, cc_power_to_renew, deal_power_to_renew)

                pledge_needed_for_renewal = (cc_power_to_renew + deal_power_to_renew) * pledge_per_pib
                pledge_repayment_value_renew = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                            pledge_needed_for_renewal, 
                                                                                                            best_duration_yrs)

                if not self.debug_mode:
                    self.renew_power(self.current_date, cc_power_to_renew, deal_power_to_renew, best_duration,
                                    pledge_needed_for_renewal, pledge_repayment_value_renew)
                else:
                    renew_args_to_return = (self.current_date, cc_power_to_renew, deal_power_to_renew, best_duration,
                                            pledge_needed_for_renewal, pledge_repayment_value_renew)

        # even if we are in debug mode, we need to step the agent b/c that updates agent internal states
        # such as current_date
        super().step()

        if self.debug_mode:
            return onboard_args_to_return, renew_args_to_return

    def post_global_step(self):
        # we can update local representation of anything else that should happen after
        # global metrics for day are aggregated
        pass

def linear(x1, y1, x2, y2, x):
        m = (y2 - y1) / (x2 - x1)
        b = y1 - m * x1
        return m * x + b


class ROIAgentDynamicOnboard(SPAgent):
    """
    The ROI agent is an agent that uses ROI forecasts to decide how much power to onboard.
    It uses a linear function to go between min/max onboard after ROI exceeds threshold

    TODO:
     [ ] - 
    """
    def __init__(self, model, id, historical_power, start_date, end_date,
                 max_sealing_throughput=constants.DEFAULT_MAX_SEALING_THROUGHPUT_PIB, 
                 min_daily_rb_onboard_pib=3, max_daily_rb_onboard_pib=12,
                 min_renewal_rate = 0.3, max_renewal_rate = 0.8,
                 fil_plus_rate=0.6, 
                 agent_optimism=4, min_roi=0.1, max_roi=0.3, debug_mode=False):
        """

        debug_mode - if True, the agent will compute the power scheduled to be onboarded/renewed, but will not actually
                     onboard/renew that power, but rather return the values.  This can be used for debugging
                     or other purposes
        """
        # note that we dont set renewal_rate, roi_threshold since we use those terms differently in this agent
        super().__init__(model, id, historical_power, start_date, end_date, max_sealing_throughput_pib=max_sealing_throughput)

        self.min_daily_rb_onboard_pib = min_daily_rb_onboard_pib
        self.max_daily_rb_onboard_pib = max_daily_rb_onboard_pib
        self.min_renewal_rate = min_renewal_rate
        self.max_renewal_rate = max_renewal_rate

        self.min_roi = min_roi
        self.max_roi = max_roi

        self.fil_plus_rate = fil_plus_rate

        self.agent_optimism = agent_optimism

        self.duration_vec_days = np.asarray([360, 360*3]).astype(np.int32)  # 1Y, 3Y, 5Y sectors are possible

        self.map_optimism_scales()

        for d in self.duration_vec_days:
            self.agent_info_df[f'roi_estimate_{d}'] = 0
        self.agent_info_df['pledge_per_pib'] = 0

        self.debug_mode = debug_mode

    ##########################################################################################
    # TODO: better to use the superclass construct, but we need to figure out 
    # how to get teh grandparent b/c we need to step on grandparent, not parent.
    # Copilot says this, but it doesnt seem to work:
    """
    class Grandparent:
        pass
    class Parent(Grandparent):
        pass
    class Child(Parent):
        pass
    child = Child()
    grandparent = super(Child, child).__class__.__bases__[0]
    """
    ##########################################################################################
    def map_optimism_scales(self):
        self.optimism_to_price_quantile_str = {
            1 : "Q05",
            2 : "Q25",
            3 : "Q50",
            4 : "Q75",
            5 : "Q95"
        }
        self.optimism_to_dayrewardspersector_quantile_str = {
            1 : "Q05",
            2 : "Q25",
            3 : "Q50",
            4 : "Q75",
            5 : "Q95"
        }

    def forecast_day_rewards_per_sector(self, forecast_start_date, forecast_length):
        k = 'day_rewards_per_sector_forecast_' + self.optimism_to_dayrewardspersector_quantile_str[self.agent_optimism]
        start_idx = self.model.global_forecast_df[pd.to_datetime(self.model.global_forecast_df['date']) == pd.to_datetime(forecast_start_date)].index[0]
        end_idx = start_idx + forecast_length
        future_rewards_per_sector = self.model.global_forecast_df.loc[start_idx:end_idx, k].values
        
        return future_rewards_per_sector
    
    def estimate_roi(self, sector_duration, date_in):
        filecoin_df_idx = self.model.filecoin_df[pd.to_datetime(self.model.filecoin_df['date']) == pd.to_datetime(date_in)].index[0]

        # NOTE: we need to use yesterday's metrics b/c today's haven't yet been aggregated by the system yet
        # prev_day_pledge_per_QAP = self.model.filecoin_df.loc[filecoin_df_idx-1, 'day_pledge_per_QAP']
        prev_day_pledge_per_QAP = self.model.estimate_pledge_for_qa_power(date_in, constants.SECTOR_SIZE/constants.PIB)

        # print(date_in, 'prev_day_pledge_per_QAP', prev_day_pledge_per_QAP)

        # TODO: make this an iterative update rather than full-estimate every day
        # NOTE: this assumes that the pledge remains constant. This is not true, but a zeroth-order approximation
        future_rewards_per_sector_estimate = self.forecast_day_rewards_per_sector(date_in, sector_duration)
        
        # get the cost per sector for the duration, which in this case is just borrowing costs
        sector_duration_yrs = sector_duration / 360.
        pledge_repayment_estimate = self.compute_repayment_amount_from_supply_discount_rate_model(date_in, prev_day_pledge_per_QAP, sector_duration_yrs)
        cost_per_sector_estimate = pledge_repayment_estimate - prev_day_pledge_per_QAP
        if prev_day_pledge_per_QAP == 0:
            roi_estimate = self.max_roi
        else:
            roi_estimate = (future_rewards_per_sector_estimate.sum() - cost_per_sector_estimate) / prev_day_pledge_per_QAP
        
        # annualize it so that we can have the same frame of reference when comparing different sector durations
        if roi_estimate < -1:
            roi_estimate_annualized = self.roi_threshold - 1  # if ROI is too low, set it so that it doesn't onboard.
                                                              # otherwise, you would take an exponent of a negative number
                                                              # to a fractional power below and get a complex number
        else:
            roi_estimate_annualized = (1.0+roi_estimate)**(1.0/sector_duration_yrs) - 1
        
        # print(roi_estimate, roi_estimate_annualized, duration_yr)
        # if np.isnan(future_rewards_per_sector_estimate.sum()) or np.isnan(prev_day_pledge_per_QAP) or np.isnan(roi_estimate) or np.isnan(roi_estimate_annualized):
        #     print(self.unique_id, future_rewards_per_sector_estimate.sum(), prev_day_pledge_per_QAP, roi_estimate, roi_estimate_annualized)

        return roi_estimate_annualized
    ##########################################################################################

    def convert_roi_to_onboard(self, estimated_roi):
        """
        Returns the amount of power to onboard and % to renew based on the delta between
        the ROI threshold and the estimated ROI
        """
        rb_to_onboard = linear(self.min_roi, self.min_daily_rb_onboard_pib, self.max_roi, self.max_daily_rb_onboard_pib, estimated_roi)
        renew_pct = linear(self.min_roi, self.min_renewal_rate, self.max_roi, self.max_renewal_rate, estimated_roi)
        return rb_to_onboard, renew_pct

    def step(self):
        roi_estimate_vec = []
        
        agent_df_idx = self.agent_info_df[pd.to_datetime(self.agent_info_df['date']) == pd.to_datetime(self.current_date)].index[0]
        for d in self.duration_vec_days:    
            roi_estimate = self.estimate_roi(d, self.current_date)            
            roi_estimate_vec.append(roi_estimate)
            self.agent_info_df.loc[agent_df_idx, 'roi_estimate_%d' % (d,)] = roi_estimate
            
        pledge_per_pib = self.model.estimate_pledge_for_qa_power(self.current_date, 1.0)
        self.agent_info_df.loc[agent_df_idx, 'pledge_per_pib'] = pledge_per_pib

        max_roi_idx = np.argmax(roi_estimate_vec)
        best_duration = self.duration_vec_days[max_roi_idx]
        best_duration_yrs = best_duration / 360.
        if roi_estimate_vec[max_roi_idx] > self.min_roi:
            rb_to_onboard, renewal_rate = self.convert_roi_to_onboard(roi_estimate_vec[max_roi_idx])
            # clip the values to the min/max
            rb_to_onboard = max(min(rb_to_onboard, self.max_daily_rb_onboard_pib), self.min_daily_rb_onboard_pib)
            renewal_rate = max(min(renewal_rate, self.max_renewal_rate), self.min_renewal_rate)
            
            qa_to_onboard = self.model.apply_qa_multiplier(rb_to_onboard * self.fil_plus_rate,
                                                       fil_plus_multipler=constants.FIL_PLUS_MULTIPLER,
                                                       date_in=self.current_date,
                                                       sector_duration_days=best_duration) + \
                        rb_to_onboard * (1-self.fil_plus_rate)
            
            pledge_needed_for_onboarding = qa_to_onboard * pledge_per_pib
            pledge_repayment_value_onboard = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                        pledge_needed_for_onboarding, 
                                                                                                        best_duration_yrs)

            if not self.debug_mode:
                self.onboard_power(self.current_date, rb_to_onboard, qa_to_onboard, best_duration,
                                pledge_needed_for_onboarding, pledge_repayment_value_onboard)
            else:
                onboard_args_to_return = (self.current_date, rb_to_onboard, qa_to_onboard, best_duration, 
                                          pledge_needed_for_onboarding, pledge_repayment_value_onboard)

            # renew available power for the same duration
            se_power_dict = self.get_se_power_at_date(self.current_date)
            # which aspects of power get renewed is dependent on the setting "renewals_setting" in the FilecoinModel object
            cc_power_to_renew = se_power_dict['se_cc_power'] * renewal_rate
            deal_power_to_renew = se_power_dict['se_deal_power'] * renewal_rate

            pledge_needed_for_renewal = (cc_power_to_renew + deal_power_to_renew) * pledge_per_pib
            pledge_repayment_value_renew = self.compute_repayment_amount_from_supply_discount_rate_model(self.current_date, 
                                                                                                        pledge_needed_for_renewal, 
                                                                                                        best_duration_yrs)

            if not self.debug_mode:
                self.renew_power(self.current_date, cc_power_to_renew, deal_power_to_renew, best_duration,
                                pledge_needed_for_renewal, pledge_repayment_value_renew)
            else:
                renew_args_to_return = (self.current_date, cc_power_to_renew, deal_power_to_renew, best_duration,
                                        pledge_needed_for_renewal, pledge_repayment_value_renew)

        # even if we are in debug mode, we need to step the agent b/c that updates agent internal states
        # such as current_date
        # grandparent = super(self.__class__, self).__class__.__bases__[0]
        # grandparent.step()
        super().step()

        if self.debug_mode:
            return onboard_args_to_return, renew_args_to_return

    def post_global_step(self):
        # we can update local representation of anything else that should happen after
        # global metrics for day are aggregated
        pass