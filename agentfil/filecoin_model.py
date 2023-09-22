import mesa
import pandas as pd
import numpy as np
from scipy.optimize import root
from datetime import timedelta
import os
import pickle

from mechafil import data, vesting, minting

from . import locking
from . import constants
from .agents import sp_agent
from . import rewards_per_sector_process
from . import fil_supply_discount_rate_process


def solve_geometric(a, n, init_guess=0.5):
    # see: https://math.stackexchange.com/a/2174287
    def f(r, a, n):
        # the geometric series 
        return a*(np.power(r,n)-1)/(r-1) - 1
    soln = root(f, init_guess, args=(a, n), method='lm')  # this method seems more reliable ...
    return soln
    
def double_check_soln(a, r, n):
    sum_val = a*(r**n-1)/(r-1)
    return 1-sum_val

def root_finder(a, n, ntry=5):
    try_idx = 0
    init_guess = 0.5
    while try_idx < ntry:
        soln = solve_geometric(a, n, init_guess)
        if soln.success:
            r = soln.x
            delta = double_check_soln(a, r, n)
            if np.isclose(delta, 0):
                return soln.x[0]
            else:
                init_guess += delta/2
        try_idx += 1
    raise ValueError("Unable to find a solution!")

def distribute_agent_power_geometric_series(num_agents, a=0.2):
    # use a geometric-series to determine the proportion of power that goes
    # to each agent
    if num_agents == 1:
        return [1.0]
    
    r = root_finder(a, num_agents)

    agent_power_distributions = []
    for i in range(num_agents):
        agent_power_pct = a*(r**i)
        agent_power_distributions.append(agent_power_pct)
    return agent_power_distributions


class FilecoinModel(mesa.Model):
    def __init__(self, n, start_date, end_date, spacescope_cfg=None,
                 max_day_onboard_rbp_pib=constants.DEFAULT_MAX_DAY_ONBOARD_RBP_PIB,
                 agent_types=None, agent_kwargs_list=None, agent_power_distributions=None,
                 compute_cs_from_networkdatastart=True, use_historical_gas=True,
                 price_process_kwargs=None, rewards_per_sector_process_kwargs=None, 
                 fil_supply_discount_rate_process_kwargs=None,
                 sdm=None, sdm_kwargs=None,  # TODO: try to generalize this as a set of possible protocol updates
                 pledge_onboard_ratio_callable=None,  # defaults to onboard ratio defined in the spec: qa_added_power / max(total_qa, baseline)
                 pledge_onboard_ratio_callable_kwargs_fn=None,
                 user_post_network_update_callables=None, user_post_network_update_callables_kwargs_list=None,
                 renewals_setting='optimistic',
                 random_seed=1234,
                 offline_historical_data=None):
        """
        n: the number of agents to instantiate
        start_date: the start date of the simulation
        end_date: the end date of the simulation
        spacescope_cfg: a dictionary of configuration parameters for the spacescope model
        max_day_onboard_rbp_pib: the maximum amount of power that can be onboarded per day, in PiB
        agent_types: a vector of the types of agents to instantiate, if None then the 
                     default is to instantiate all agents as SPAgent
        agent_kwargs_list: a list of dictionaries, each dictionary contains keywords to configure
                            the instantiated agent.
        agent_power_distributions: a vector of the proportion of power that goes to each agent. If None,
                            will be computed using the default function `distribute_agent_power_geometric_series`
        compute_cs_from_networkdatastart: if True, then the circulating supply is computed from the
                               start of network data (2021-03-15). If False, then the circulating supply
                               is computed from the simulation start date and pre-seeded with
                               historical data for dates prior to the simulation start date.
                               In backtesting, it was observed that compute_cs_from_start=True
                               leads to a more accurate simulation of the circulating supply, so this
                               is the default option. This option has no effect on the power predictions, 
                               only circulating supply.
        use_historical_gas: if True, gas prices are seeded from historical data. If False, gas prices
                            are computed as a constant average value. In backtesting, it was observed
                            that use_historical_gas=False leads to better backtesting results. This option
                            is only relevant when compute_cs_from_start=True.
        price_process_kwargs: a dictionary of keyword arguments to pass to the price process
        minting_process_kwargs: a dictionary of keyword arguments to pass to the minting process
        fil_supply_discount_rate_process_kwargs: a dictionary of keyword arguments to pass to the FIL supply discount rate process
        sdm: a function handle which computes a SDM multiplier. It must have the following signature:
            def sdm(...):
        sdm_kwargs: a dictionary of keyword arguments to pass to the SDM function
        renewals_setting: a string that specifies how to compute the QA power. Valid options are: 'optimistic' (default) and 'conservative'
            1 - In the optimistic setting, renewals are computed for both QA and CC power. This is to capture the sentiment that as Deal sectors
                expire, even though an explicit renewal is not made, it is in effect with more deals coming online, or the deal being renewed
                through the normal channel of expire + re-onboard.
            2 - In the conservative setting, renewals are only computed for CC sectors.  NOTE that this is not properly implemented yet, b/c
                currently, the CC power is renewed.  However, the CC contains CC sectors & QA sectors (without the QA multiplier), so in effect
                this is not as conservative as you might expect if ONLY CC sectors were indeed being renewed.
        random_seed: the random seed to use for the simulation
        """
        if spacescope_cfg is None:
            raise ValueError("spacescope_cfg must be specified")
        data.setup_spacescope(spacescope_cfg)

        self.num_agents = n
        self.max_day_onboard_rbp_pib = max_day_onboard_rbp_pib
        self.MAX_DAY_ONBOARD_RBP_PIB_PER_AGENT = self.max_day_onboard_rbp_pib / n
        self.MIN_DAY_ONBOARD_RBP_PIB_PER_AGENT = constants.MIN_SECTORS_ONBOARD * constants.SECTOR_SIZE / constants.PIB
        # ensure that min/max values don't clash w/ the # of agents
        assert self.num_agents * self.MIN_DAY_ONBOARD_RBP_PIB_PER_AGENT <= self.max_day_onboard_rbp_pib, \
            "max_day_onboard_rbp_pib is too small for the number of agents"
        
        # TODO: I think these should become configuration objects, this is getting a bit wary ... 
        self.price_process_kwargs = price_process_kwargs
        if self.price_process_kwargs is None:
            self.price_process_kwargs = {}
        self.rewards_per_sector_process_kwargs = rewards_per_sector_process_kwargs
        if self.rewards_per_sector_process_kwargs is None:
            self.rewards_per_sector_process_kwargs = {}

        self.fil_supply_discount_rate_process_kwargs = fil_supply_discount_rate_process_kwargs
        if self.fil_supply_discount_rate_process_kwargs is None:
            self.fil_supply_discount_rate_process_kwargs = {}
        self.user_post_network_update_callables = user_post_network_update_callables
        self.user_post_network_update_callables_kwargs_list = user_post_network_update_callables_kwargs_list
        if self.user_post_network_update_callables_kwargs_list is None:
            self.user_post_network_update_callables_kwargs_list = []
            if self.user_post_network_update_callables is not None:
                for _ in range(len(self.user_post_network_update_callables)):
                    self.user_post_network_update_callables_kwargs_list.append({})

        self.sdm = sdm
        self.sdm_kwargs = sdm_kwargs

        self.pledge_onboard_ratio_callable = pledge_onboard_ratio_callable
        if self.pledge_onboard_ratio_callable is None:
            self.pledge_onboard_ratio_callable = locking.spec_onboard_ratio
        self.pledge_onboard_ratio_callable_kwargs_fn = pledge_onboard_ratio_callable_kwargs_fn
        if self.pledge_onboard_ratio_callable_kwargs_fn is None:
            self.pledge_onboard_ratio_callable_kwargs_fn = locking.noop
        
        self.renewals_setting = renewals_setting

        self.random_seed = random_seed
        self.schedule = mesa.time.SimultaneousActivation(self)

        if agent_power_distributions is None:
            self.agent_power_distributions = distribute_agent_power_geometric_series(n)
        else:
            self.agent_power_distributions = agent_power_distributions

        self.compute_cs_from_networkdatastart = compute_cs_from_networkdatastart
        self.use_historical_gas = use_historical_gas

        # if not compute_cs_from_networkdatastart:
        #     raise ValueError("Value only True supported for now ...")

        self.start_date = start_date
        self.current_date = start_date
        self.end_date = end_date
        self.sim_len = (self.end_date - self.start_date).days

        self.start_day = (self.start_date - constants.NETWORK_DATA_START).days
        self.current_day = (self.current_date - constants.NETWORK_DATA_START).days

        self.agents = []
        self.rbp0 = None
        self.qap0 = None

        self._validate(agent_kwargs_list)

        self._setup_network_configuration()

        self._initialize_network_description_df()

        historical_stats, scheduled_df = self._load_historical_data(offline_historical_data)
        self._download_historical_data(historical_stats, scheduled_df)
        
        
        self._seed_agents(agent_types=agent_types, agent_kwargs_list=agent_kwargs_list)
        self._fast_forward_to_simulation_start()
        self._zero_agent_rewards()  # do this to establish causality between agent actions and rewards

        self._setup_global_forecasts()

    def apply_qa_multiplier(self,
                            power_in, 
                            fil_plus_multipler=constants.FIL_PLUS_MULTIPLER, 
                            date_in=None,
                            sector_duration_days=365):
        if self.sdm is not None:
            sdm_kwargs = {} if self.sdm_kwargs is None else self.sdm_kwargs
            sdm_multiplier = self.sdm(date_in=date_in, sector_duration_days=sector_duration_days, **sdm_kwargs)
            return power_in * sdm_multiplier * fil_plus_multipler
        else: # Assume no SDM
            return power_in * fil_plus_multipler

    def step(self):
        # update global forecasts
        self._update_global_forecasts()

        # step agents
        self.schedule.step()

        day_macro_info = self._compute_macro(self.current_date)
        self._update_power_metrics(day_macro_info)
        self._update_minting()
        self._aggregate_terminations()
        self._update_sched_expire_pledge(self.current_date)
        self._update_circulating_supply()
        self._update_generated_quantities()

        self._step_post_network_updates()
        self._step_user_post_network_updates()

        self._update_agents()
        # update any other inputs to agents

        # increment counters
        self.current_date += timedelta(days=1)
        self.current_day += 1

    def _setup_network_configuration(self):
        self.lock_target = 0.3  # the default lock target

    def _setup_global_forecasts(self):
        self.global_forecast_df = pd.DataFrame()
        self.global_forecast_df['date'] = self.filecoin_df['date']

        # need to forecast this many days after the simulation end date because
        # agents will be making decisions uptil the end of simulation with future forecasts
        final_date = self.filecoin_df['date'].iloc[-1]
        remaining_len = constants.MAX_SECTOR_DURATION_DAYS
        future_dates = [final_date + timedelta(days=i) for i in range(1, remaining_len + 1)]
        self.global_forecast_df = pd.concat([self.global_forecast_df, pd.DataFrame({'date': future_dates})], ignore_index=True)
        
        #self.price_process = price_process.PriceProcess(self, **self.price_process_kwargs)
        self.minting_process = rewards_per_sector_process.RewardsPerSectorProcess(self, **self.rewards_per_sector_process_kwargs)
        #self.capital_inflow_process = capital_inflow_process.CapitalInflowProcess(self, **self.capital_inflow_process_kwargs)
        self.fil_supply_discount_rate_process = fil_supply_discount_rate_process.FILSupplyDiscountRateProcess(self, **self.fil_supply_discount_rate_process_kwargs)

    def _update_global_forecasts(self):
        # call stuff here that should be updated before agents make decisions
        # self.price_process.step()
        self.minting_process.step()

        # since the forecasts are updated *before* the agents make decisions, we need to
        # use the previous day's circulating supply when setting the discount rate for today
        # NOTE that this is only applicable in the case where the discount rate is computed adaptively
        filecoin_df_idx = self.filecoin_df[self.filecoin_df['date'] == self.current_date].index[0]
        prev_day_idx = filecoin_df_idx - 1
        prev_circ_supply = self.filecoin_df.loc[prev_day_idx, 'circ_supply']
        self.fil_supply_discount_rate_process.step(circ_supply=prev_circ_supply)
    
    def _step_post_network_updates(self):
        # call stuff here that should be run after all network statistics have been updated
        # self.capital_inflow_process.step()
        pass

    def _step_user_post_network_updates(self):
        # call any user defined stuff here that should be run after all network statistics have been updated
        if self.user_post_network_update_callables is not None:
            for user_post_network_update_callable, user_post_network_update_callable_kwargs in zip(self.user_post_network_update_callables, self.user_post_network_update_callables_kwargs_list):
                user_post_network_update_callable(self, **user_post_network_update_callable_kwargs)  # by passing self to this, the user can access any of the model's attributes & update them!

    def estimate_pledge_for_qa_power(self, date_in, qa_power_pib):
        """
        Computes the required pledge for a given date and desired QA power to onboard.
        The general use-case for this function will be that for a given day, the agent
        is deciding whether to onboard a certain amount of power. The agent can call
        this function to determine the required pledge to onboard that amount of power,
        and then make a decision.

        Note that in the step function above, the agent is first called to make a decision.
        After all agents have made a decisions, the model aggregates the decisions for that 
        day and computes econometrics that depend on the agent's decisions, such as the total
        network QAP, circulating supply, etc. So, for a given time t, this function uses econometrics
        from time t-1 to estimate the pledge requirement.

        If the agent decides to pledge power, the actual required pledge is computed after all global
        metrics are computed and this is logged in the agent's accounting_df dataframe.

        Parameters
        ----------
        date_in : datetime.date
            date for which to compute the pledge
        qa_power_pib : float
            QA power to onboard
        """
        
        filecoin_df_idx = self.filecoin_df[self.filecoin_df['date'] == date_in].index[0]
        prev_day_idx = filecoin_df_idx - 1
        
        prev_circ_supply = self.filecoin_df.loc[prev_day_idx, 'circ_supply']
        prev_total_qa_power_pib = self.filecoin_df.loc[prev_day_idx, 'total_qa_power_eib'] * 1024.0
        prev_baseline_power_pib = self.filecoin_df.loc[prev_day_idx, 'network_baseline'] / constants.PIB
        prev_day_network_reward = self.filecoin_df.loc[prev_day_idx, 'day_network_reward']

        if qa_power_pib == 0:
            # if no-onboards, then keep the pledge estimate as the previous day to keep the trajectory smooth
            pledge_estimate = self.filecoin_df.loc[prev_day_idx, 'day_pledge_per_QAP'] * (qa_power_pib / constants.SECTOR_SIZE)
        else:
            if self.pledge_onboard_ratio_callable_kwargs_fn is None:
                pledge_onboard_ratio_callable_kwargs = {}
            else:
                pledge_onboard_ratio_callable_kwargs = self.pledge_onboard_ratio_callable_kwargs_fn(
                    date_in, self.filecoin_df.iloc[prev_day_idx], self.lock_target
                )

            pledge_estimate = locking.compute_new_pledge_for_added_power(
                prev_day_network_reward,
                prev_circ_supply,
                qa_power_pib * constants.PIB,
                prev_total_qa_power_pib * constants.PIB,
                prev_baseline_power_pib * constants.PIB,
                self.lock_target,
                self.pledge_onboard_ratio_callable,
                pledge_onboard_ratio_callable_kwargs
            )
        return pledge_estimate
    
    def get_discount_rate_pct(self, date_in):
        day_idx = self.filecoin_df[self.filecoin_df['date'] == date_in].index[0]
        return self.filecoin_df.loc[day_idx, 'discount_rate_pct']

    def borrow_FIL_with_discount_rate(self, date_in, borrow_amt_FIL, duration_yrs, compounding_freq_yrs=1):
        discount_rate_pct = self.get_discount_rate_pct(date_in)
        discount_rate = discount_rate_pct / 100.0
        return borrow_amt_FIL / (1.0 + discount_rate / compounding_freq_yrs) ** (compounding_freq_yrs * duration_yrs)

    def _validate(self, agent_kwargs_list):
        if self.start_date < constants.NETWORK_DATA_START:
            raise ValueError(f"start_date must be after {constants.NETWORK_DATA_START}")
        if self.end_date < self.start_date:
            raise ValueError("end_date must be after start_date")
        assert len(self.agent_power_distributions) == self.num_agents
        assert np.isclose(sum(self.agent_power_distributions), 1.0)

        if agent_kwargs_list is not None:
            assert len(agent_kwargs_list) == self.num_agents

    def _load_historical_data(self, offline_historical_data):
        if offline_historical_data is None:
            return None, None
        else:
            with open(offline_historical_data, 'rb') as f:
                offline_historical_data = pickle.load(f)
            historical_stats = offline_historical_data['historical_stats']
            scheduled_df = offline_historical_data['scheduled_df']
        return historical_stats, scheduled_df

    def _download_historical_data(self, historical_stats=None, scheduled_df=None):
        # TODO: have an offline method to speed this up ... otherwise takes 30s to initialize the model
        if historical_stats is None:
            historical_stats = data.get_historical_network_stats(
                constants.NETWORK_DATA_START,
                self.start_date,
                self.end_date
            )
        if scheduled_df is None:
            scheduled_df = data.query_sector_expirations(constants.NETWORK_DATA_START, self.end_date)
        # get the fields necessary to seed the agents into a separate dataframe that is time-aligned
        historical_stats['date'] = pd.to_datetime(historical_stats['date']).dt.date
        scheduled_df['date'] = scheduled_df['date'].dt.date
        merged_df = historical_stats.merge(scheduled_df, on='date', how='inner')
        
        # NOTE: consider using scheduled_expire_rb rather than total_rb??
        self.df_historical = merged_df[
            [
                'date', 
                'day_onboarded_rb_power_pib', 'extended_rb', 'total_rb', 'terminated_rb',
                'day_onboarded_qa_power_pib', 'extended_qa', 'total_qa', 'terminated_qa',
                'total_raw_power_eib', 'total_qa_power_eib',
            ]
        ]
        self.df_historical = self.df_historical[self.df_historical['date'] <= (self.start_date-timedelta(days=1))]
        # rename columns for internal consistency
        self.df_historical = self.df_historical.rename(
            columns={
                'extended_rb': 'extended_rb_pib',
                'extended_qa': 'extended_qa_pib',
                'total_rb': 'sched_expire_rb_pib',
                'total_qa': 'sched_expire_qa_pib',
                'terminated_rb': 'terminated_rb_pib',
                'terminated_qa': 'terminated_qa_pib',
            }
        )
        scheduled_df = scheduled_df.rename(
            columns={
                'total_rb': 'sched_expire_rb_pib',
                'total_qa': 'sched_expire_qa_pib',
            }
        )
        
        final_date_historical = historical_stats.iloc[-1]['date']
        self.df_future = scheduled_df[scheduled_df['date'] >= final_date_historical][['date', 'sched_expire_rb_pib', 'sched_expire_qa_pib']]

        self.rbp0 = merged_df.iloc[0]['total_raw_power_eib']
        self.qap0 = merged_df.iloc[0]['total_qa_power_eib']
        self.max_date_se_power = self.df_future.iloc[-1]['date']

        # this vector starts from the first day of the simulation
        known_scheduled_pledge_release_vec = scheduled_df["total_pledge"].values
        start_idx = self.filecoin_df[self.filecoin_df['date'] == scheduled_df.iloc[0]['date']].index[0]
        end_idx = start_idx + len(known_scheduled_pledge_release_vec) - 1
        self.filecoin_df['scheduled_pledge_release'] = 0
        self.filecoin_df.loc[start_idx:end_idx, 'scheduled_pledge_release'] = known_scheduled_pledge_release_vec
        
        self.zero_cum_capped_power = data.get_cum_capped_rb_power(constants.NETWORK_DATA_START)

    def _seed_agents(self, agent_types=None, agent_kwargs_list=None):
        for ii in range(self.num_agents):
            agent_power_pct = self.agent_power_distributions[ii]
            print('seeding agent', ii, 'with power pct', agent_power_pct)
            agent_historical_df = self.df_historical.drop('date', axis=1) * agent_power_pct
            agent_historical_df['date'] = self.df_historical['date']
            agent_future_df = self.df_future.drop('date', axis=1) * agent_power_pct
            agent_future_df['date'] = self.df_future['date']
            agent_scheduled_pledge_release_df = self.filecoin_df[['scheduled_pledge_release']] * agent_power_pct
            agent_scheduled_pledge_release_df['date'] = self.filecoin_df['date']
            agent_seed = {
                'historical_power': agent_historical_df,
                'future_se_power': agent_future_df,
                'scheduled_pledge_release': agent_scheduled_pledge_release_df,
                'agent_power_pct': agent_power_pct,
            }
            if agent_types is not None:
                agent_cls = agent_types[ii]
            else:
                agent_cls = sp_agent.SPAgent
            
            agent_kwargs = {}
            if agent_kwargs_list is not None:
                agent_kwargs = agent_kwargs_list[ii]
            agent = agent_cls(self, ii, agent_seed, self.start_date, self.end_date, **agent_kwargs)

            self.schedule.add(agent)
            self.agents.append(
                {
                    'agent_power_pct': agent_power_pct,
                    'agent': agent,
                }
            )

    def _initialize_network_description_df(self):
        self.filecoin_df = pd.DataFrame()
        
        # precompute columns which do not depend on inputs
        self.filecoin_df['date'] = pd.date_range(constants.NETWORK_DATA_START, self.end_date, freq='D')[:-1]
        self.filecoin_df['date'] = self.filecoin_df['date'].dt.date
        days_offset = (constants.NETWORK_DATA_START - constants.NETWORK_START).days
        self.filecoin_df['days'] = np.arange(days_offset, len(self.filecoin_df)+days_offset)

        self.filecoin_df['network_baseline'] = minting.compute_baseline_power_array(constants.NETWORK_DATA_START, self.end_date)
        vest_df = vesting.compute_vesting_trajectory_df(constants.NETWORK_DATA_START, self.end_date)
        self.filecoin_df["cum_simple_reward"] = self.filecoin_df["days"].pipe(minting.cum_simple_minting)
        
        self.filecoin_df = self.filecoin_df.merge(vest_df, on='date', how='inner')
        self.filecoin_df['burn_from_terminations'] = 0

        # generated quantities which are only updated from simulation_start
        self.filecoin_df['day_pledge_per_QAP'] = 0.0
        self.filecoin_df['day_rewards_per_sector'] = 0.0
        self.filecoin_df['discount_rate_pct'] = 0.0

        # for debugging
        self.filecoin_df['pledge_delta'] = 0
        self.filecoin_df['reward_delta'] = 0
    
    def _fast_forward_to_simulation_start(self):
        current_date = constants.NETWORK_DATA_START
        day_power_stats_vec = []
        print('Fast forwarding power to simulation start date...', self.start_date)
        while current_date < self.start_date:
            day_power_stats = self._compute_macro(current_date)
            day_power_stats_vec.append(day_power_stats)

            current_date += timedelta(days=1)
        power_stats_df = pd.DataFrame(day_power_stats_vec)
        final_historical_data_idx = power_stats_df.index[-1]

        # create cumulative statistics which are needed to compute minting
        power_stats_df['total_raw_power_eib'] = power_stats_df['day_network_rbp_pib'].cumsum() / 1024.0 + self.rbp0
        power_stats_df['total_qa_power_eib'] = power_stats_df['day_network_qap_pib'].cumsum() / 1024.0  + self.qap0

        # ##########################################################################################
        # NOTE: encapsulate this into a function b/c it needs to be done iteratively in the model step function
        filecoin_df_subset = self.filecoin_df[self.filecoin_df['date'] < self.start_date]
        # TODO: better error messages
        assert len(filecoin_df_subset) == len(power_stats_df)
        assert power_stats_df.iloc[0]['date'] == filecoin_df_subset.iloc[0]['date']
        assert power_stats_df.iloc[-1]['date'] == filecoin_df_subset.iloc[-1]['date']
        
        power_stats_df['capped_power'] = (constants.EIB*power_stats_df['total_raw_power_eib']).clip(upper=filecoin_df_subset['network_baseline'])
        power_stats_df['cum_capped_power'] = power_stats_df['capped_power'].cumsum() + self.zero_cum_capped_power
        power_stats_df['network_time'] = power_stats_df['cum_capped_power'].pipe(minting.network_time)
        power_stats_df['cum_baseline_reward'] = power_stats_df['network_time'].pipe(minting.cum_baseline_reward)
        power_stats_df['cum_network_reward'] = power_stats_df['cum_baseline_reward'].values + filecoin_df_subset['cum_simple_reward'].values
        power_stats_df['day_network_reward'] = power_stats_df['cum_network_reward'].diff().fillna(method='backfill')
        power_stats_df['day_simple_reward'] = filecoin_df_subset['cum_simple_reward'].diff().fillna(method='backfill')
        # ##########################################################################################

        # concatenate w/ NA for rest of the simulation so that the merge doesn't delete the data in the master DF
        remaining_power_stats_df = pd.DataFrame(np.nan, index=range(self.sim_len), columns=power_stats_df.columns)
        remaining_power_stats_df['date'] = pd.date_range(self.start_date, self.end_date, freq='D')[:-1]

        power_stats_df = pd.concat([power_stats_df, remaining_power_stats_df], ignore_index=True)

        # for proper merging, we need to convert to datetime
        power_stats_df['date'] = pd.to_datetime(power_stats_df['date'])
        self.filecoin_df['date'] = pd.to_datetime(self.filecoin_df['date'])

        # merge this into the master filecoin description dataframe
        self.filecoin_df = self.filecoin_df.merge(power_stats_df, on='date', how='outer')
        self.filecoin_df['date'] = self.filecoin_df['date'].dt.date

        # add in future SE power
        se_power_stats_vec = []
        print('Computing Scheduled Expirations from: ', self.current_date, ' to: ', self.max_date_se_power)
        # pbar = tqdm(total=(self.max_date_se_power - self.current_date).days)
        while current_date < self.max_date_se_power:
            se_power_stats = self._compute_macro(current_date)
            se_power_stats_vec.append(se_power_stats)

            current_date += timedelta(days=1)
            # pbar.update(1)
        se_power_stats_df = pd.DataFrame(se_power_stats_vec)

        l = len(se_power_stats_df)
        self.filecoin_df.loc[final_historical_data_idx+1:final_historical_data_idx+l, ['day_sched_expire_rbp_pib']] = se_power_stats_df['day_sched_expire_rbp_pib'].values
        self.filecoin_df.loc[final_historical_data_idx+1:final_historical_data_idx+l, ['day_sched_expire_qap_pib']] = se_power_stats_df['day_sched_expire_qap_pib'].values

        #####################################################################################
        # initialize the circulating supply
        print('Initializing circulating supply...')
        supply_df = data.query_supply_stats(constants.NETWORK_DATA_START, self.start_date)
        start_idx = self.filecoin_df[self.filecoin_df['date'] == supply_df.iloc[0]['date']].index[0]
        end_idx = self.filecoin_df[self.filecoin_df['date'] == supply_df.iloc[-1]['date']].index[0]
        
        self.filecoin_df['disbursed_reserve'] = (17066618961773411890063046 * 10**-18)  # constant across time.
        self.filecoin_df['network_gas_burn'] = 0

        # internal CS metrics are initialized to 0 and only filled after the simulation start date
        self.filecoin_df['day_locked_pledge'] = 0
        self.filecoin_df['day_renewed_pledge'] = 0
        self.filecoin_df['network_locked_pledge'] = 0
        self.filecoin_df['network_locked_reward'] = 0
        self.filecoin_df['original_pledge'] = 0
        self.filecoin_df['renewal_rate'] = 0
        self.daily_burnt_fil = supply_df["burnt_fil"].diff().mean()

        # test consistency between mechaFIL and agentFIL by computing CS from beginning of simulation
        # rather than after simulation start only
        print('Updating circulating supply statistics...')
        if self.compute_cs_from_networkdatastart:
            if self.use_historical_gas:
                self.filecoin_df.loc[start_idx:end_idx, 'network_gas_burn'] = supply_df['burnt_fil'].values
            locked_fil_zero = supply_df.iloc[start_idx]['locked_fil']
            self.filecoin_df.loc[start_idx, "network_locked_pledge"] = locked_fil_zero / 2.0
            self.filecoin_df.loc[start_idx, "network_locked_reward"] = locked_fil_zero / 2.0
            self.filecoin_df.loc[start_idx, "network_locked"] = locked_fil_zero
            self.filecoin_df.loc[start_idx, 'circ_supply'] = supply_df.iloc[start_idx]['circulating_fil']
            print('Updating circulating supply statistics... --> start_date:', self.filecoin_df.loc[start_idx+1, 'date'])
            for day_idx in range(start_idx+1, end_idx):
                date_in = self.filecoin_df.loc[day_idx, 'date']
                self._update_sched_expire_pledge(date_in, update_filecoin_df=False)
                self._update_circulating_supply(update_day=day_idx)
                self._update_generated_quantities(update_day=day_idx)
                self._update_agents(update_day=day_idx)
            print('Finished updating CS.  Final date ->', self.filecoin_df.loc[end_idx-1, 'date'])       
        else:
            # NOTE: cum_network_reward was computed above from power inputs, use that rather than historical data
            # NOTE: vesting was computed above and is a static model, so use the precomputed vesting information
            # self.filecoin_df.loc[start_idx:end_idx, 'total_vest'] = supply_df['vested_fil'].values
            self.filecoin_df.loc[start_idx:end_idx, 'network_locked'] = supply_df['locked_fil'].values
            self.filecoin_df.loc[start_idx:end_idx, 'network_gas_burn'] = supply_df['burnt_fil'].values
            
            # compute circulating supply rather than overwriting it with historical data to be consistent
            # with minting model
            self.filecoin_df.loc[start_idx:end_idx, 'circ_supply'] = (
                self.filecoin_df.loc[start_idx:end_idx, 'disbursed_reserve']
                + self.filecoin_df.loc[start_idx:end_idx, 'cum_network_reward']  # from the minting_model
                + self.filecoin_df.loc[start_idx:end_idx, 'total_vest']  # from vesting_model
                - self.filecoin_df.loc[start_idx:end_idx, 'network_locked']  # from simulation loop
                - self.filecoin_df.loc[start_idx:end_idx, 'network_gas_burn']  # comes from user inputs
            )
            locked_fil_zero = self.filecoin_df.loc[final_historical_data_idx, ["network_locked"]].values[0]
            self.filecoin_df.loc[final_historical_data_idx+1, ["network_locked_pledge"]] = locked_fil_zero / 2.0
            self.filecoin_df.loc[final_historical_data_idx+1, ["network_locked_reward"]] = locked_fil_zero / 2.0
            self.filecoin_df.loc[final_historical_data_idx+1, ["network_locked"]] = locked_fil_zero

        ############################################################################################################
        
    def _compute_macro(self, date_in):
        # aggregate Power statistics from agents
        total_rb_delta, total_qa_delta = 0, 0
        total_onboarded_rb_delta, total_renewed_rb_delta, total_se_rb_delta, total_terminated_rb_delta = 0, 0, 0, 0
        total_onboarded_qa_delta, total_renewed_qa_delta, total_se_qa_delta, total_terminated_qa_delta = 0, 0, 0, 0
        for agent_info in self.agents:
            agent = agent_info['agent']
            agent_day_power_stats = agent.get_power_at_date(date_in)
            
            total_onboarded_rb_delta += agent_day_power_stats['day_onboarded_rb_power_pib']
            total_onboarded_qa_delta += agent_day_power_stats['day_onboarded_qa_power_pib']
            
            total_renewed_rb_delta += agent_day_power_stats['extended_rb_pib']
            total_renewed_qa_delta += agent_day_power_stats['extended_qa_pib']

            total_se_rb_delta += agent_day_power_stats['sched_expire_rb_pib']
            total_se_qa_delta += agent_day_power_stats['sched_expire_qa_pib']

            total_terminated_rb_delta += agent_day_power_stats['terminated_rb_pib']
            total_terminated_qa_delta += agent_day_power_stats['terminated_qa_pib']

        total_rb_delta += (total_onboarded_rb_delta + total_renewed_rb_delta - total_se_rb_delta - total_terminated_rb_delta)
        total_qa_delta += (total_onboarded_qa_delta + total_renewed_qa_delta - total_se_qa_delta - total_terminated_qa_delta)

        out_dict = {
            'date': date_in,
            'day_onboarded_rbp_pib': total_onboarded_rb_delta,
            'day_onboarded_qap_pib': total_onboarded_qa_delta,
            'day_renewed_rbp_pib': total_renewed_rb_delta,
            'day_renewed_qap_pib': total_renewed_qa_delta,
            'day_sched_expire_rbp_pib': total_se_rb_delta,
            'day_sched_expire_qap_pib': total_se_qa_delta,
            'day_terminated_rbp_pib': total_terminated_rb_delta,
            'day_terminated_qap_pib': total_terminated_qa_delta,
            'day_network_rbp_pib': total_rb_delta,
            'day_network_qap_pib': total_qa_delta,
        }
        return out_dict

    def _update_power_metrics(self, day_macro_info, day_idx=None):
        day_idx = self.current_day if day_idx is None else day_idx

        self.filecoin_df.loc[day_idx, 'day_onboarded_rbp_pib'] = day_macro_info['day_onboarded_rbp_pib']
        ## FLAG: div / 0 protection
        self.filecoin_df.loc[day_idx, 'day_onboarded_qap_pib'] = max(day_macro_info['day_onboarded_qap_pib'], constants.MIN_VALUE)
        
        self.filecoin_df.loc[day_idx, 'day_renewed_rbp_pib'] = day_macro_info['day_renewed_rbp_pib']
        self.filecoin_df.loc[day_idx, 'day_renewed_qap_pib'] = day_macro_info['day_renewed_qap_pib']
        self.filecoin_df.loc[day_idx, 'day_sched_expire_rbp_pib'] = day_macro_info['day_sched_expire_rbp_pib']
        self.filecoin_df.loc[day_idx, 'day_sched_expire_qap_pib'] = day_macro_info['day_sched_expire_qap_pib']
        self.filecoin_df.loc[day_idx, 'day_terminated_rbp_pib'] = day_macro_info['day_terminated_rbp_pib']
        self.filecoin_df.loc[day_idx, 'day_terminated_qap_pib'] = day_macro_info['day_terminated_qap_pib']
        self.filecoin_df.loc[day_idx, 'day_network_rbp_pib'] = day_macro_info['day_network_rbp_pib']
        self.filecoin_df.loc[day_idx, 'day_network_qap_pib'] = day_macro_info['day_network_qap_pib']

        ## FLAG: div / 0 protection
        self.filecoin_df.loc[day_idx, 'total_raw_power_eib'] = max(self.filecoin_df.loc[day_idx, 'day_network_rbp_pib'] / 1024.0 + self.filecoin_df.loc[day_idx-1, 'total_raw_power_eib'], constants.MIN_VALUE)
        self.filecoin_df.loc[day_idx, 'total_qa_power_eib'] = max(self.filecoin_df.loc[day_idx, 'day_network_qap_pib'] / 1024.0 + self.filecoin_df.loc[day_idx-1, 'total_qa_power_eib'], constants.MIN_VALUE)

    def _update_minting(self, day_idx=None):
        day_idx = self.current_day if day_idx is None else day_idx
        baseline_pwr = self.filecoin_df.loc[day_idx, 'network_baseline']

        capped_power = min(constants.EIB*self.filecoin_df.loc[day_idx, 'total_raw_power_eib'], baseline_pwr)
        cum_capped_power = capped_power + self.filecoin_df.loc[day_idx-1, 'cum_capped_power']
        self.filecoin_df.loc[day_idx, 'capped_power'] = capped_power
        self.filecoin_df.loc[day_idx, 'cum_capped_power'] = cum_capped_power
        network_time = minting.network_time(cum_capped_power)
        self.filecoin_df.loc[day_idx, 'network_time'] = network_time
        cum_baseline_reward = minting.cum_baseline_reward(network_time)
        self.filecoin_df.loc[day_idx, 'cum_baseline_reward'] = cum_baseline_reward
        cum_network_reward = cum_baseline_reward + self.filecoin_df.loc[day_idx, 'cum_simple_reward']
        self.filecoin_df.loc[day_idx, 'cum_network_reward'] = cum_network_reward
        self.filecoin_df.loc[day_idx, 'day_network_reward'] = cum_network_reward - self.filecoin_df.loc[day_idx-1, 'cum_network_reward']
        self.filecoin_df.loc[day_idx, 'day_simple_reward'] = self.filecoin_df.loc[day_idx, 'cum_simple_reward'] - self.filecoin_df.loc[day_idx-1, 'cum_simple_reward']

    def _aggregate_terminations(self, update_day=None):
        day_idx = self.current_day if update_day is None else update_day
        date_in = self.filecoin_df.loc[day_idx, "date"]

        for agent_info in self.agents:
            agent = agent_info['agent']
            agent_df_idx = agent.accounting_df[agent.accounting_df['date'] == date_in].index[0]
            self.filecoin_df.loc[day_idx, "burn_from_terminations"] += agent.accounting_df.loc[agent_df_idx, "termination_burned_FIL"]

            # pledge shift is taken care of when the agent calls _release_pledge

            # reward vesting is taken care of within the agent

    def _release_pledge(self, date_from, date_to, FIL_amt):
        date_from_idx = self.filecoin_df[self.filecoin_df['date'] == date_from].index[0] if date_from is not None else None
        date_to_idx = self.filecoin_df[self.filecoin_df['date'] == date_to].index[0]

        self.filecoin_df.loc[date_to_idx, 'scheduled_pledge_release'] += FIL_amt
        if date_from_idx is not None:
            self.filecoin_df.loc[date_from_idx, 'scheduled_pledge_release'] -= FIL_amt
        # print(f"Released {FIL_amt} FIL from pledge from {date_from} to {date_to}")

    def _update_sched_expire_pledge(self, date_in, update_filecoin_df=True):
        """
        Update the scheduled pledge release for the day. Track this for each agent.

        Parameters
        ----------
        date_in : str
            date to update
        update_filecoin_df : bool
            whether to update the filecoin_df with the new information.  This should be set to
            True for all user cases. In the special case where we are fast-forwarding the
            simulation, we set it to False because the relevant information was already updated.
        """
        day_idx = self.filecoin_df[self.filecoin_df['date'] == date_in].index[0]
        
        total_qa = self.filecoin_df.loc[day_idx, "total_qa_power_eib"] * constants.EIB
        baseline_power = self.filecoin_df.loc[day_idx, "network_baseline"]
        day_network_reward = self.filecoin_df.loc[day_idx, "day_network_reward"]
        prev_circ_supply = self.filecoin_df.loc[day_idx-1, "circ_supply"]
        
        aggregate_rr = 1.0
        for agent_info in self.agents:
            agent = agent_info['agent']
            agent_day_power_stats = agent.get_power_at_date(date_in)
        
            day_onboarded_qap = agent_day_power_stats['day_onboarded_qa_power_pib'] * constants.PIB
            day_renewed_qap = agent_day_power_stats['extended_qa_pib'] * constants.PIB
            
            if self.pledge_onboard_ratio_callable_kwargs_fn is None:
                pledge_onboard_ratio_callable_kwargs = {}
            else:
                pledge_onboard_ratio_callable_kwargs = self.pledge_onboard_ratio_callable_kwargs_fn(
                    date_in, self.filecoin_df.iloc[day_idx-1], self.lock_target
                )
            
            # compute total pledge this agent will locked
            onboards_locked = locking.compute_new_pledge_for_added_power(
                day_network_reward,
                prev_circ_supply,
                day_onboarded_qap,
                total_qa,
                baseline_power,
                self.lock_target,
                self.pledge_onboard_ratio_callable,
                pledge_onboard_ratio_callable_kwargs
            )
            renews_locked = locking.compute_new_pledge_for_added_power(
                day_network_reward,
                prev_circ_supply,
                day_renewed_qap,
                total_qa,
                baseline_power,
                self.lock_target,
                self.pledge_onboard_ratio_callable,
                pledge_onboard_ratio_callable_kwargs
            )

            # get the original pledge that was scheduled to expire on this day 
            original_pledge = agent.accounting_df.loc[day_idx, "scheduled_pledge_release"]

            # scale it by the amount that was renewed
            agent_day_renewed_qap = agent_day_power_stats['extended_qa_pib'] * constants.PIB
            agent_day_se_qap = agent_day_power_stats['sched_expire_qa_pib'] * constants.PIB
            if agent_day_se_qap == 0 and agent_day_renewed_qap > 0:
                agent_rr = 1.0
            elif agent_day_se_qap < 0 or agent_day_renewed_qap == 0:
                agent_rr = 0.0
            else:
                agent_rr = agent_day_renewed_qap / agent_day_se_qap
            agent_rr = np.clip(agent_rr, 0, 1)
            aggregate_rr *= agent_rr

            original_pledge_for_renew = original_pledge * agent_rr
            renews_locked = max(original_pledge_for_renew, renews_locked)

            onboarded_qa_duration = agent_day_power_stats['day_onboarded_qa_duration']
            renewed_qa_duration = agent_day_power_stats['extended_qa_duration']
            
            # only update the vector if it is within the simulation range
            agent.accounting_df.loc[day_idx, "onboard_pledge_FIL"] += onboards_locked
            if day_idx + onboarded_qa_duration < len(self.filecoin_df):
                if update_filecoin_df:
                    self.filecoin_df.loc[day_idx + onboarded_qa_duration, "scheduled_pledge_release"] += onboards_locked
                    
                    agent.accounting_df.loc[day_idx + onboarded_qa_duration, "onboard_scheduled_pledge_release_FIL"] += onboards_locked
                    agent.accounting_df.loc[day_idx + onboarded_qa_duration, "scheduled_pledge_release"] += onboards_locked

            agent.accounting_df.loc[day_idx, "renew_pledge_FIL"] += renews_locked
            if day_idx + renewed_qa_duration < len(self.filecoin_df):
                if update_filecoin_df:
                    self.filecoin_df.loc[day_idx + renewed_qa_duration, "scheduled_pledge_release"] += renews_locked
                    
                    agent.accounting_df.loc[day_idx + renewed_qa_duration, "renew_scheduled_pledge_release_FIL"] += renews_locked
                    agent.accounting_df.loc[day_idx + renewed_qa_duration, "scheduled_pledge_release"] += renews_locked

            # for debugging
            self.filecoin_df.loc[day_idx, "original_pledge"] += original_pledge_for_renew
        self.filecoin_df.loc[day_idx, "renewal_rate"] = aggregate_rr
    
    def _update_circulating_supply(self, update_day=None):
        day_idx = self.current_day if update_day is None else update_day
        current_date = self.filecoin_df.iloc[day_idx]["date"]

        network_QAP = self.filecoin_df.iloc[day_idx]["total_qa_power_eib"] * constants.EIB                  # in bytes
        network_baseline = self.filecoin_df.iloc[day_idx]["network_baseline"]                               # in bytes
        day_network_reward = self.filecoin_df.iloc[day_idx]["day_network_reward"]
        
        prev_network_locked_reward = self.filecoin_df.iloc[day_idx-1]["network_locked_reward"]
        prev_network_locked_pledge = self.filecoin_df.iloc[day_idx-1]["network_locked_pledge"]
        prev_network_locked = self.filecoin_df.iloc[day_idx-1]["network_locked"]

        prev_circ_supply = self.filecoin_df["circ_supply"].iloc[day_idx-1]

        # do this per agent rather than in aggregate to account for different renewal rates
        # of different agents properly
        pledge_delta = 0
        day_locked_pledge = 0
        day_renewed_pledge = 0
        for agent_info in self.agents:
            agent = agent_info['agent']

            day_locked_pledge += (agent.accounting_df["onboard_pledge_FIL"].iloc[day_idx] + agent.accounting_df["renew_pledge_FIL"].iloc[day_idx])
            day_renewed_pledge += agent.accounting_df["renew_pledge_FIL"].iloc[day_idx]
        
            # print(current_date)
            # print(agent.agent_info_df.iloc[0])
            ix = np.where(agent.t == current_date)[0][0]
            agent_onboarded_qap = agent.onboarded_power[ix][1].pib * constants.PIB
            agent_renewed_qap = agent.renewed_power[ix][1].pib * constants.PIB
            agent_se_qap = agent.scheduled_expire_power[ix][1].pib * constants.PIB

            # agent_rr = agent_renewed_qap / agent_se_qap
            if agent_se_qap == 0 and agent_renewed_qap > 0:
                agent_rr = 1.0
            elif agent_se_qap < 0 or agent_renewed_qap == 0:
                agent_rr = 0.0
            else:
                agent_rr = agent_renewed_qap / agent_se_qap
            agent_rr = np.clip(agent_rr, 0, 1)
            
            agent_accounting_df_idx = agent.accounting_df[pd.to_datetime(agent.accounting_df['date']) == pd.to_datetime(current_date)].index[0]
            agent_scheduled_pledge_release = agent.accounting_df["scheduled_pledge_release"].iloc[agent_accounting_df_idx]

            if self.pledge_onboard_ratio_callable_kwargs_fn is None:
                pledge_onboard_ratio_callable_kwargs = {}
            else:
                pledge_onboard_ratio_callable_kwargs = self.pledge_onboard_ratio_callable_kwargs_fn(
                    current_date, self.filecoin_df.iloc[day_idx-1], self.lock_target
                )

            agent_pledge_delta = locking.compute_day_delta_pledge(
                day_network_reward,
                prev_circ_supply,
                agent_onboarded_qap,
                agent_renewed_qap,
                network_QAP,
                network_baseline,
                agent_rr,
                agent_scheduled_pledge_release,
                self.lock_target,
                onboard_ratio_callable=self.pledge_onboard_ratio_callable,
                onboard_ratio_callable_kwargs=pledge_onboard_ratio_callable_kwargs
            )
            pledge_delta += agent_pledge_delta

        # Compute daily change in block rewards collateral
        day_locked_rewards = locking.compute_day_locked_rewards(day_network_reward)
        day_reward_release = locking.compute_day_reward_release(prev_network_locked_reward)
        reward_delta = day_locked_rewards - day_reward_release
        
        # Update dataframe
        self.filecoin_df.loc[day_idx, "pledge_delta"] = pledge_delta
        self.filecoin_df.loc[day_idx, "reward_delta"] = reward_delta

        # TODO: do we need to update day_locked_pledge from terminations? don't think so, but need to confirm
        self.filecoin_df.loc[day_idx, "day_locked_pledge"] = day_locked_pledge 
        self.filecoin_df.loc[day_idx, "day_renewed_pledge"] = day_renewed_pledge
        self.filecoin_df.loc[day_idx, "network_locked_pledge"] = (
            prev_network_locked_pledge + pledge_delta
        )
        self.filecoin_df.loc[day_idx, "network_locked_reward"] = (
            prev_network_locked_reward + reward_delta
        )
        self.filecoin_df.loc[day_idx, "network_locked"] = (
            prev_network_locked + pledge_delta + reward_delta
        )
        # Update gas burnt
        if self.filecoin_df.loc[day_idx, "network_gas_burn"] == 0.0:
            self.filecoin_df["network_gas_burn"].iloc[day_idx] = (
                self.filecoin_df["network_gas_burn"].iloc[day_idx - 1] + self.daily_burnt_fil
            )
        # Find circulating supply balance and update
        circ_supply = (
            self.filecoin_df["disbursed_reserve"].iloc[day_idx]  # from initialise_circulating_supply_df
            + self.filecoin_df["cum_network_reward"].iloc[day_idx]  # from the minting_model
            + self.filecoin_df["total_vest"].iloc[day_idx]  # from vesting_model
            - self.filecoin_df["network_locked"].iloc[day_idx]  # from simulation loop
            - self.filecoin_df["network_gas_burn"].iloc[day_idx]  # comes from user inputs
            - self.filecoin_df['burn_from_terminations'].iloc[day_idx]  # from agent decisions
        )
        self.filecoin_df.loc[day_idx, "circ_supply"] = max(circ_supply, 0)

    def _update_generated_quantities(self, update_day=None):
        day_idx = self.current_day if update_day is None else update_day
        update_date = self.filecoin_df.iloc[day_idx]['date']

        # add ROI to trajectory df
        day_locked_pledge = self.filecoin_df.loc[day_idx, 'day_locked_pledge']
        day_renewed_pledge = self.filecoin_df.loc[day_idx, 'day_renewed_pledge']
        # FLAG: avoid division by zero - does it make sense to do this?
        day_onboarded_power_QAP = max(self.filecoin_df.loc[day_idx, "day_onboarded_qap_pib"] * constants.PIB, constants.MIN_VALUE)   # in bytes
        self.filecoin_df.loc[day_idx, 'day_pledge_per_QAP'] = constants.SECTOR_SIZE * (day_locked_pledge-day_renewed_pledge)/day_onboarded_power_QAP

        # print(update_date, day_locked_pledge, day_renewed_pledge, day_onboarded_power_QAP, self.filecoin_df.loc[day_idx, 'day_pledge_per_QAP'])

        day_network_reward = self.filecoin_df.iloc[day_idx]["day_network_reward"]
        # FLAG: avoid division by zero - does it make sense to do this?
        network_QAP = max(self.filecoin_df.iloc[day_idx]["total_qa_power_eib"] * constants.EIB, constants.MIN_VALUE)                  # in bytes
        self.filecoin_df.loc[day_idx, 'day_rewards_per_sector'] = constants.SECTOR_SIZE * day_network_reward / network_QAP
        
    def _get_agent_power_proportion(self, update_day=None):
        day_idx = self.current_day if update_day is None else update_day
        date_in = self.filecoin_df.iloc[day_idx]['date']

        total_network_qap = self.filecoin_df.iloc[day_idx]["total_qa_power_eib"] * constants.EIB
        agent_power_proportion_vec = np.zeros(len(self.agents))
        for ii, agent_info in enumerate(self.agents):
            agent = agent_info['agent']

            total_agent_qap = agent.get_active_qa_power_at_date(date_in) * constants.PIB
            agent_power_proportion = min(total_agent_qap/total_network_qap, 1.0) # account for numerical issues
            agent_power_proportion_vec[ii] = agent_power_proportion
            
        # print(agent_power_proportion_vec, sum(agent_power_proportion_vec))
        agent_power_proportion_vec = agent_power_proportion_vec / sum(agent_power_proportion_vec)
        agentid2power_proportion = {}
        for ii, agent_info in enumerate(self.agents):
            agent = agent_info['agent']
            agentid2power_proportion[agent.unique_id] = agent_power_proportion_vec[ii]

        return agent_power_proportion_vec, agentid2power_proportion

    def _update_agents(self, update_day=None):
        day_idx = self.current_day if update_day is None else update_day
        date_in = self.filecoin_df.iloc[day_idx]['date']

        total_day_rewards = self.filecoin_df.iloc[day_idx]["day_network_reward"]
        agent_reward_ratio_vec, _ = self._get_agent_power_proportion(update_day=day_idx)

        debug_agent_qap_sum = 0
        for ii, agent_info in enumerate(self.agents):
            agent = agent_info['agent']
            
            total_agent_qap = agent.get_active_qa_power_at_date(date_in) * constants.PIB
            debug_agent_qap_sum += total_agent_qap
            # agent_reward_ratio = min(total_agent_qap/total_network_qap, 1.0) # account for numerical issues
            agent_reward_ratio = agent_reward_ratio_vec[ii]
            agent_reward = total_day_rewards * agent_reward_ratio

            agent_accounting_df = agent.accounting_df
            accounting_df_idx = agent_accounting_df[agent_accounting_df['date'] == date_in].index[0]

            # TODO: think about updating the disburse_rewards function to do this in a more functional way
            # 25 % vests immediately
            agent_accounting_df.loc[accounting_df_idx, 'reward_FIL'] += agent_reward * 0.25
            # remainder vests linearly over the next 180 days
            agent_accounting_df.loc[accounting_df_idx+1:accounting_df_idx+180, 'reward_FIL'] += (agent_reward * 0.75)/180

            agent_accounting_df.loc[accounting_df_idx, 'full_reward_for_power_FIL'] += agent_reward

            agent.post_global_step()
        # this delta shoudl be close to zero
        # print(date_in, (total_network_qap-debug_agent_qap_sum)/constants.EIB)

    def _zero_agent_rewards(self):
        for agent_info in self.agents:
            agent = agent_info['agent']
            agent.accounting_df['reward_FIL'] = 0.0

    def save_data(self, output_dir):
        self.filecoin_df.to_csv(os.path.join(output_dir, 'filecoin_df.csv'))
        for agent_info in self.agents:
            agent = agent_info['agent']
            agent.save_data(output_dir)

        # # TODO: is this necessary?
        # output_fp = os.path.join(output_dir, 'simulation.pkl')
        # dill.dump(self, output_fp)

